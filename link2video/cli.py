"""Unified CLI entrypoint combining Tencent Meeting download, processing, and FFmpeg muxing."""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from . import __version__
from ._utils import ensure_utf8_stdio
from .downloader import TRACK_PRIORITY, _sanitize_filename, download_meeting
from .media import find_binary
from .muxer import mux_media
from .processor import process_all

logger = logging.getLogger(__name__)


@dataclass
class MuxOptions:
    """Options passed to the FFmpeg mux/burn-in stage."""
    burn_subs: bool = False
    font_name: str = "Microsoft YaHei"
    font_size: int = 15
    video_preset: str = "veryfast"
    crf: int = 23


def _resolve_output_path(output: Optional[str | Path], default_stem: str) -> Path:
    if not output:
        return Path.cwd() / f"{default_stem}.mp4"
    p = Path(output)
    raw = str(output)
    if p.is_dir() or raw.endswith(("/", "\\")) or not p.suffix:
        return p / f"{default_stem}.mp4"
    return p


def _require_ffmpeg(ffmpeg_dir: Optional[str | Path]) -> None:
    if not find_binary("ffmpeg", ffmpeg_dir=ffmpeg_dir):
        raise FileNotFoundError(
            "ffmpeg binary not found. Install FFmpeg on PATH, set FFMPEG_DIR, or pass --ffmpeg-dir."
        )


def _run_mux_stage(
    video_path: Path,
    out_path: Path,
    srt_path: Optional[Path],
    meta_path: Optional[Path],
    ffmpeg_dir: Optional[str | Path],
    opts: MuxOptions,
    show_progress: bool = False,
) -> Path:
    return mux_media(
        video_path=video_path,
        output_path=out_path,
        srt_path=srt_path,
        meta_path=meta_path,
        ffmpeg_dir=ffmpeg_dir,
        burn_subs=opts.burn_subs,
        font_name=opts.font_name,
        font_size=opts.font_size,
        video_preset=opts.video_preset,
        crf=opts.crf,
        show_progress=show_progress,
    )


def run_url_pipeline(
    url: str,
    output: Optional[str | Path] = None,
    track: str = "composite",
    cookie: Optional[str] = None,
    ffmpeg_dir: Optional[str | Path] = None,
    keep_temp: bool = False,
    work_dir: Optional[str | Path] = None,
    mux_options: Optional[MuxOptions] = None,
) -> Path:
    """Download a Tencent Meeting recording and produce a muxed MP4/MKV with subtitles & chapters."""
    _require_ffmpeg(ffmpeg_dir)
    opts = mux_options or MuxOptions()

    if work_dir or keep_temp:
        base_work = Path(work_dir) if work_dir else Path.cwd() / "output"
        base_work.mkdir(parents=True, exist_ok=True)
        return _execute_url_in_dir(
            url=url,
            work_path=base_work,
            output=output,
            track=track,
            cookie=cookie,
            ffmpeg_dir=ffmpeg_dir,
            opts=opts,
        )

    with tempfile.TemporaryDirectory(prefix="link2video_") as tmp:
        return _execute_url_in_dir(
            url=url,
            work_path=Path(tmp),
            output=output,
            track=track,
            cookie=cookie,
            ffmpeg_dir=ffmpeg_dir,
            opts=opts,
        )


def _execute_url_in_dir(
    url: str,
    work_path: Path,
    output: Optional[str | Path],
    track: str,
    cookie: Optional[str],
    ffmpeg_dir: Optional[str | Path],
    opts: MuxOptions,
) -> Path:
    print(f"[1/3] Resolving and downloading Tencent Meeting recording: {url}")
    dl = download_meeting(
        url=url,
        output_dir=work_path / "raw",
        track=track,
        cookie=cookie,
    )
    safe_stem = _sanitize_filename(dl.title)
    out_path = _resolve_output_path(output, safe_stem)

    if dl.transcript_path is None and dl.timeline_path is None:
        logger.warning("Transcript and timeline are unavailable for this recording; copying raw video.")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(dl.video_path, out_path)
        return out_path

    print("[2/3] Generating subtitles (.srt) and chapter metadata (.ffmeta)...")
    srt_p, meta_p, stats = process_all(
        transcript_path=dl.transcript_path,
        timeline_path=dl.timeline_path,
        video_path=dl.video_path,
        out_dir=work_path / "processed",
        base_name=safe_stem,
        ffmpeg_dir=ffmpeg_dir,
    )
    print(f"      Subtitles: {stats.srt_cues}, Chapters: {stats.chapters}")

    mode_label = "burning hardcoded subtitles (re-encoding)" if opts.burn_subs else "losslessly muxing soft subtitles"
    print(f"[3/3] Running FFmpeg ({mode_label}) -> {out_path}")
    return _run_mux_stage(
        video_path=dl.video_path,
        out_path=out_path,
        srt_path=srt_p,
        meta_path=meta_p,
        ffmpeg_dir=ffmpeg_dir,
        opts=opts,
        show_progress=opts.burn_subs,
    )


def _execute_local_in_dir(
    work_path: Path,
    video_path: Path,
    transcript_path: Optional[str | Path],
    timeline_path: Optional[str | Path],
    out_path: Path,
    ffmpeg_dir: Optional[str | Path],
    opts: MuxOptions,
) -> Path:
    srt_p, meta_p, stats = process_all(
        transcript_path=transcript_path,
        timeline_path=timeline_path,
        video_path=video_path,
        out_dir=work_path,
        base_name=video_path.stem,
        ffmpeg_dir=ffmpeg_dir,
    )
    print(f"[1/2] Subtitles: {stats.srt_cues}, Chapters: {stats.chapters}")
    mode_label = "Burning hardcoded subtitles (re-encoding)" if opts.burn_subs else "Losslessly muxing"
    print(f"[2/2] {mode_label} -> {out_path}")
    return _run_mux_stage(
        video_path=video_path,
        out_path=out_path,
        srt_path=srt_p,
        meta_path=meta_p,
        ffmpeg_dir=ffmpeg_dir,
        opts=opts,
        show_progress=opts.burn_subs,
    )


def run_local_pipeline(
    video_path: str | Path,
    transcript_path: Optional[str | Path] = None,
    timeline_path: Optional[str | Path] = None,
    output: Optional[str | Path] = None,
    ffmpeg_dir: Optional[str | Path] = None,
    keep_temp: bool = False,
    mux_options: Optional[MuxOptions] = None,
) -> Path:
    """Process local transcript/timeline files and mux them into a local video."""
    _require_ffmpeg(ffmpeg_dir)
    v_path = Path(video_path)
    out_path = _resolve_output_path(output, f"{v_path.stem}_subtitled")
    opts = mux_options or MuxOptions()

    if keep_temp:
        return _execute_local_in_dir(
            out_path.parent / f"{out_path.stem}_assets",
            v_path, transcript_path, timeline_path, out_path, ffmpeg_dir, opts,
        )

    with tempfile.TemporaryDirectory(prefix="link2video_local_") as tmp:
        return _execute_local_in_dir(
            Path(tmp),
            v_path, transcript_path, timeline_path, out_path, ffmpeg_dir, opts,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="link2video",
        description="Convert Tencent Meeting recording links (or local assets) into MP4/MKV with subtitles and chapters.",
    )
    parser.add_argument("url", nargs="?", help="Tencent Meeting share URL (https://meeting.tencent.com/crm/... or /cw/...)")
    parser.add_argument("-o", "--output", default=None, help="Output video path (.mp4/.mkv) or directory.")
    parser.add_argument("-c", "--cookie", default=None, help="Optional browser Cookie header for login-protected recordings.")
    parser.add_argument(
        "--track",
        choices=list(TRACK_PRIORITY.keys()),
        default="composite",
        help="Preferred video track: composite (default), screen, or speaker.",
    )
    parser.add_argument(
        "--burn", "--hard-sub",
        dest="burn_subs",
        action="store_true",
        help="Burn subtitles directly into video frames (hardsub, re-encodes via libx264).",
    )
    parser.add_argument("--font-name", default="Microsoft YaHei", help="Font name for burned-in subtitles (used with --burn).")
    parser.add_argument("--font-size", type=int, default=15, help="Font size for burned-in subtitles (default: 15, used with --burn).")
    parser.add_argument("--preset", default="veryfast", help="x264 encoding preset (default: veryfast, used with --burn).")
    parser.add_argument("--crf", type=int, default=23, help="x264 CRF quality (default: 23, lower is higher quality, used with --burn).")
    parser.add_argument("--ffmpeg-dir", default=None, help="Directory containing ffmpeg/ffprobe binaries (or set FFMPEG_DIR).")
    parser.add_argument("--keep-temp", action="store_true", help="Keep downloaded raw video and generated .srt/.ffmeta files.")
    parser.add_argument("--work-dir", default=None, help="Working directory for intermediate files (used with --keep-temp).")

    local_group = parser.add_argument_group("Local file mode (--local)")
    local_group.add_argument("--local", action="store_true", help="Mux local video, transcript, and timeline files directly.")
    local_group.add_argument("-v", "--video", default=None, help="Local video file path (.mp4).")
    local_group.add_argument("-t", "--transcript", default=None, help="Local transcript file path (.md/.txt).")
    local_group.add_argument("-l", "--timeline", default=None, help="Local timeline file path (.md/.txt).")
    parser.add_argument("--version", action="version", version=f"link2video {__version__}")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    ensure_utf8_stdio()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    parser = build_parser()
    args = parser.parse_args(argv)
    opts = MuxOptions(
        burn_subs=args.burn_subs,
        font_name=args.font_name,
        font_size=args.font_size,
        video_preset=args.preset,
        crf=args.crf,
    )

    try:
        if args.local or (args.url is None and args.video is not None):
            if not args.video:
                parser.error("--local mode requires -v/--video.")
            if not args.transcript and not args.timeline:
                parser.error("--local mode requires at least -t/--transcript or -l/--timeline.")
            final_path = run_local_pipeline(
                video_path=args.video,
                transcript_path=args.transcript,
                timeline_path=args.timeline,
                output=args.output,
                ffmpeg_dir=args.ffmpeg_dir,
                keep_temp=args.keep_temp,
                mux_options=opts,
            )
        elif args.url:
            final_path = run_url_pipeline(
                url=args.url,
                output=args.output,
                track=args.track,
                cookie=args.cookie,
                ffmpeg_dir=args.ffmpeg_dir,
                keep_temp=args.keep_temp,
                work_dir=args.work_dir,
                mux_options=opts,
            )
        else:
            parser.print_help()
            return 2

        print(f"[OK] Done! Output: {final_path}")
        return 0
    except (FileNotFoundError, PermissionError, ValueError, RuntimeError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

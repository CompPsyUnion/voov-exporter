"""Unified CLI entrypoint combining Tencent Meeting download, processing, and FFmpeg muxing."""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

from . import __version__
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
            "未找到 ffmpeg 可执行文件。请安装 FFmpeg 并加入 PATH，"
            "或设置环境变量 FFMPEG_DIR，或通过 --ffmpeg-dir 指定目录。"
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
    password: Optional[str] = None,
    cookie: Optional[str] = None,
    ffmpeg_dir: Optional[str | Path] = None,
    keep_temp: bool = False,
    work_dir: Optional[str | Path] = None,
    password_prompt: Callable[[str], str] = input,
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
            password=password,
            cookie=cookie,
            ffmpeg_dir=ffmpeg_dir,
            password_prompt=password_prompt,
            opts=opts,
        )

    with tempfile.TemporaryDirectory(prefix="link2video_") as tmp:
        return _execute_url_in_dir(
            url=url,
            work_path=Path(tmp),
            output=output,
            track=track,
            password=password,
            cookie=cookie,
            ffmpeg_dir=ffmpeg_dir,
            password_prompt=password_prompt,
            opts=opts,
        )


def _execute_url_in_dir(
    url: str,
    work_path: Path,
    output: Optional[str | Path],
    track: str,
    password: Optional[str],
    cookie: Optional[str],
    ffmpeg_dir: Optional[str | Path],
    password_prompt: Callable[[str], str],
    opts: MuxOptions,
) -> Path:
    print(f"[1/3] 正在解析并下载腾讯会议录制: {url}")
    dl = download_meeting(
        url=url,
        output_dir=work_path / "raw",
        track=track,
        password=password,
        cookie=cookie,
        password_prompt=password_prompt,
    )
    safe_stem = _sanitize_filename(dl.title)
    out_path = _resolve_output_path(output, safe_stem)

    if dl.transcript_path is None and dl.timeline_path is None:
        logger.warning("该会议未提供逐字稿与章节数据，直接输出原视频。")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(dl.video_path, out_path)
        return out_path

    print("[2/3] 正在生成字幕 (.srt) 与章节元数据 (.ffmeta)...")
    srt_p, meta_p, stats = process_all(
        transcript_path=dl.transcript_path,
        timeline_path=dl.timeline_path,
        video_path=dl.video_path,
        out_dir=work_path / "processed",
        base_name=safe_stem,
        ffmpeg_dir=ffmpeg_dir,
    )
    print(f"      字幕条数: {stats.srt_cues}, 章节数: {stats.chapters}")

    mode_label = "烧录硬字幕（需重编码）" if opts.burn_subs else "无损封装软字幕"
    print(f"[3/3] 正在使用 FFmpeg {mode_label} -> {out_path}")
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
    print(f"[1/2] 字幕条数: {stats.srt_cues}, 章节数: {stats.chapters}")
    mode_label = "烧录硬字幕（需重编码）" if opts.burn_subs else "无损封装"
    print(f"[2/2] 正在{mode_label} -> {out_path}")
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
        description="将腾讯会议录制链接（或本地导出文件）合成为带字幕和可跳转章节的 MP4/MKV 视频。",
    )
    parser.add_argument("url", nargs="?", help="腾讯会议回放分享链接 (https://meeting.tencent.com/crm/... or /cw/...)")
    parser.add_argument("-o", "--output", default=None, help="输出视频路径 (.mp4/.mkv) 或输出目录。")
    parser.add_argument("-p", "--password", default=None, help="会议访问密码（若需密码且未传此参数，将在终端提示输入）。")
    parser.add_argument("-c", "--cookie", default=None, help="可选浏览器 Cookie（用于需要登录权限的录制）。")
    parser.add_argument(
        "--track",
        choices=list(TRACK_PRIORITY.keys()),
        default="composite",
        help="视频轨道偏好：composite(合成视频，默认)、screen(共享屏幕)、speaker(说话人)。",
    )
    parser.add_argument(
        "--burn", "--hard-sub",
        dest="burn_subs",
        action="store_true",
        help="将字幕直接烧录压制进视频画面（硬字幕，所有播放器直接可见，需要重编码）。",
    )
    parser.add_argument("--font-name", default="Microsoft YaHei", help="硬字幕字体名称（配合 --burn 使用）。")
    parser.add_argument("--font-size", type=int, default=15, help="硬字幕字号（默认 15，配合 --burn 使用）。")
    parser.add_argument("--preset", default="veryfast", help="x264 编码速度预设（默认 veryfast，配合 --burn 使用）。")
    parser.add_argument("--crf", type=int, default=23, help="x264 画质 CRF 值（默认 23，越小越清晰，配合 --burn 使用）。")
    parser.add_argument("--ffmpeg-dir", default=None, help="包含 ffmpeg/ffprobe 的目录（也可通过 FFMPEG_DIR 环境变量指定）。")
    parser.add_argument("--keep-temp", action="store_true", help="保留下载的原视频及生成的 .srt / .ffmeta 中间文件。")
    parser.add_argument("--work-dir", default=None, help="指定中间文件存放目录（配合 --keep-temp 使用）。")

    local_group = parser.add_argument_group("本地文件模式 (--local)")
    local_group.add_argument("--local", action="store_true", help="使用本地视频、逐字稿与时间轴文件直接合成。")
    local_group.add_argument("-v", "--video", default=None, help="本地视频文件路径 (.mp4)。")
    local_group.add_argument("-t", "--transcript", default=None, help="本地逐字稿文件路径 (.md/.txt)。")
    local_group.add_argument("-l", "--timeline", default=None, help="本地时间轴文件路径 (.md/.txt)。")
    parser.add_argument("--version", action="version", version=f"link2video {__version__}")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
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
                parser.error("--local 模式必须通过 -v/--video 指定本地视频路径。")
            if not args.transcript and not args.timeline:
                parser.error("--local 模式必须至少指定 -t/--transcript 或 -l/--timeline。")
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
                password=args.password,
                cookie=args.cookie,
                ffmpeg_dir=args.ffmpeg_dir,
                keep_temp=args.keep_temp,
                work_dir=args.work_dir,
                mux_options=opts,
            )
        else:
            parser.print_help()
            return 2

        print(f"[OK] 完成！输出文件: {final_path}")
        return 0
    except (FileNotFoundError, PermissionError, ValueError, RuntimeError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

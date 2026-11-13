"""FFmpeg muxer supporting lossless soft-subtitle muxing and burned-in hard subtitles."""

from __future__ import annotations

import argparse
import logging
import subprocess
from pathlib import Path
from typing import List, Optional

from .media import find_binary

logger = logging.getLogger(__name__)

_SUBTITLE_CODEC_BY_EXT = {
    ".mkv": "srt",
    ".mp4": "mov_text",
    ".m4v": "mov_text",
    ".mov": "mov_text",
}


def _subtitle_codec_for(output_path: Path) -> str:
    return _SUBTITLE_CODEC_BY_EXT.get(output_path.suffix.lower(), "mov_text")


def _escape_filter_value(value: str) -> str:
    """Escape special characters for FFmpeg filter option values."""
    return (
        value.replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "\\'")
        .replace("[", "\\[")
        .replace("]", "\\]")
    )


def _build_burn_filter(srt_filename: str, font_name: str, font_size: int) -> str:
    esc_name = _escape_filter_value(srt_filename)
    esc_font = _escape_filter_value(font_name)
    style = (
        f"FontName={esc_font},FontSize={font_size},"
        "PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
        "BorderStyle=1,Outline=1.5,Shadow=0.8,MarginV=18"
    )
    return f"subtitles=filename='{esc_name}':charenc=UTF-8:force_style='{style}'"


def build_mux_command(
    ffmpeg_bin: str,
    video_path: Path,
    output_path: Path,
    srt_path: Optional[Path] = None,
    meta_path: Optional[Path] = None,
    subtitle_lang: str = "chi",
    subtitle_title: str = "中文字幕",
    overwrite: bool = True,
    burn_subs: bool = False,
    font_name: str = "Microsoft YaHei",
    font_size: int = 15,
    video_preset: str = "veryfast",
    crf: int = 23,
) -> List[str]:
    """Construct the FFmpeg command for soft-subtitle muxing or hardcoded subtitle burn-in."""
    cmd: List[str] = [ffmpeg_bin, "-y" if overwrite else "-n", "-i", str(video_path)]
    next_idx = 1

    meta_idx: Optional[int] = None
    if meta_path is not None:
        meta_idx = next_idx
        cmd.extend(["-i", str(meta_path)])
        next_idx += 1

    embed_soft_srt = srt_path is not None and not burn_subs
    srt_idx: Optional[int] = None
    if embed_soft_srt:
        srt_idx = next_idx
        cmd.extend(["-i", str(srt_path)])

    cmd.extend(["-map", "0:v", "-map", "0:a?"])
    if srt_idx is not None:
        cmd.extend(["-map", f"{srt_idx}:0"])

    if meta_idx is not None:
        cmd.extend(["-map_metadata", str(meta_idx), "-map_chapters", str(meta_idx)])

    if burn_subs and srt_path is not None:
        vf = _build_burn_filter(srt_path.name, font_name=font_name, font_size=font_size)
        cmd.extend([
            "-vf", vf,
            "-c:v", "libx264",
            "-preset", video_preset,
            "-crf", str(crf),
            "-pix_fmt", "yuv420p",
            "-c:a", "copy",
        ])
    else:
        cmd.extend(["-c:v", "copy", "-c:a", "copy"])

    if srt_idx is not None:
        sub_codec = _subtitle_codec_for(output_path)
        cmd.extend([
            "-c:s", sub_codec,
            "-disposition:s:0", "default",
            "-metadata:s:s:0", f"language={subtitle_lang}",
            "-metadata:s:s:0", f"title={subtitle_title}",
        ])

    if output_path.suffix.lower() in {".mp4", ".m4v", ".mov"}:
        cmd.extend(["-movflags", "+faststart"])

    cmd.append(str(output_path))
    return cmd


def mux_media(
    video_path: str | Path,
    output_path: str | Path,
    srt_path: Optional[str | Path] = None,
    meta_path: Optional[str | Path] = None,
    subtitle_lang: str = "chi",
    subtitle_title: str = "中文字幕",
    ffmpeg_dir: Optional[str | Path] = None,
    overwrite: bool = True,
    burn_subs: bool = False,
    font_name: str = "Microsoft YaHei",
    font_size: int = 15,
    video_preset: str = "veryfast",
    crf: int = 23,
    show_progress: bool = False,
) -> Path:
    """Mux video with SRT subtitles (soft track or burned-in) and/or FFMETADATA chapters."""
    v_path = Path(video_path).resolve()
    if not v_path.is_file():
        raise FileNotFoundError(f"Video file not found: {v_path}")

    s_path = Path(srt_path).resolve() if srt_path is not None else None
    if s_path is not None and not s_path.is_file():
        raise FileNotFoundError(f"Subtitle file not found: {s_path}")

    m_path = Path(meta_path).resolve() if meta_path is not None else None
    if m_path is not None and not m_path.is_file():
        raise FileNotFoundError(f"Chapter metadata file not found: {m_path}")

    if s_path is None and m_path is None:
        raise ValueError("At least one of srt_path or meta_path must be provided to mux.")

    ffmpeg_bin = find_binary("ffmpeg", ffmpeg_dir=ffmpeg_dir)
    if not ffmpeg_bin:
        raise FileNotFoundError(
            "ffmpeg binary not found. Install FFmpeg on PATH, set FFMPEG_DIR, or pass --ffmpeg-dir."
        )

    out_path = Path(output_path).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = build_mux_command(
        ffmpeg_bin=ffmpeg_bin,
        video_path=v_path,
        output_path=out_path,
        srt_path=s_path,
        meta_path=m_path,
        subtitle_lang=subtitle_lang,
        subtitle_title=subtitle_title,
        overwrite=overwrite,
        burn_subs=burn_subs,
        font_name=font_name,
        font_size=font_size,
        video_preset=video_preset,
        crf=crf,
    )
    logger.info("Running FFmpeg mux command: %s", " ".join(cmd))

    # Run inside s_path.parent when burning subtitles so FFmpeg never trips on Windows drive colons.
    run_cwd = str(s_path.parent) if (burn_subs and s_path is not None) else None
    if show_progress:
        res = subprocess.run(cmd, cwd=run_cwd, check=False)
        if res.returncode != 0:
            raise RuntimeError(f"FFmpeg muxing failed (exit {res.returncode}).")
    else:
        res = subprocess.run(
            cmd, cwd=run_cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False
        )
        if res.returncode != 0:
            tail = (res.stderr or res.stdout or "").strip()[-1500:]
            raise RuntimeError(f"FFmpeg muxing failed (exit {res.returncode}):\n{tail}")

    return out_path


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    parser = argparse.ArgumentParser(description="Mux MP4/MKV with soft or burned-in SRT subtitles and chapters.")
    parser.add_argument("-v", "--video", required=True, help="Input video file path.")
    parser.add_argument("-s", "--srt", default=None, help="Input SRT subtitle file path.")
    parser.add_argument("-m", "--meta", default=None, help="Input FFMETADATA chapter file path.")
    parser.add_argument("-o", "--output", required=True, help="Output video path (.mp4 or .mkv).")
    parser.add_argument("--burn", "--hard-sub", dest="burn_subs", action="store_true",
                        help="Burn subtitles directly into video frames (re-encodes video via libx264).")
    parser.add_argument("--font-name", default="Microsoft YaHei", help="Font name for burned-in subtitles.")
    parser.add_argument("--font-size", type=int, default=15, help="Font size for burned-in subtitles (default: 15).")
    parser.add_argument("--preset", default="veryfast", help="x264 preset when --burn is used (default: veryfast).")
    parser.add_argument("--crf", type=int, default=23, help="x264 CRF quality when --burn is used (default: 23).")
    parser.add_argument("--lang", default="chi", help="Subtitle ISO-639 language tag (default: chi).")
    parser.add_argument("--sub-title", default="中文字幕", help="Subtitle stream title metadata.")
    parser.add_argument("--ffmpeg-dir", default=None, help="Directory containing ffmpeg/ffprobe binaries.")
    args = parser.parse_args()

    if not args.srt and not args.meta:
        parser.error("At least one of --srt or --meta must be provided.")

    out = mux_media(
        video_path=args.video,
        output_path=args.output,
        srt_path=args.srt,
        meta_path=args.meta,
        subtitle_lang=args.lang,
        subtitle_title=args.sub_title,
        ffmpeg_dir=args.ffmpeg_dir,
        burn_subs=args.burn_subs,
        font_name=args.font_name,
        font_size=args.font_size,
        video_preset=args.preset,
        crf=args.crf,
        show_progress=args.burn_subs,
    )
    print(f"[OK] Muxed media -> {out}")


if __name__ == "__main__":
    main()

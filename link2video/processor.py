"""Orchestrate transcript-to-SRT and timeline-to-FFMETADATA processing."""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ._utils import read_lines, write_text
from .chapters import generate_ffmetadata, parse_timeline
from .media import probe_video_duration
from .subtitles import generate_srt, parse_transcript


@dataclass
class ProcessStats:
    """Diagnostics collected during transcript and timeline processing."""
    paragraphs: int = 0
    speakers: Dict[str, int] = field(default_factory=dict)
    skipped_transcript_lines: List[str] = field(default_factory=list)
    srt_cues: int = 0
    chapters: int = 0
    skipped_timeline_lines: List[str] = field(default_factory=list)
    video_duration: Optional[float] = None


def process_all(
    transcript_path: Optional[str | Path] = None,
    timeline_path: Optional[str | Path] = None,
    video_dur: Optional[float] = None,
    video_path: Optional[str | Path] = None,
    out_dir: Optional[str | Path] = None,
    base_name: str = "output",
    ffmpeg_dir: Optional[str | Path] = None,
) -> Tuple[Optional[Path], Optional[Path], ProcessStats]:
    """Process transcript and/or timeline files, saving outputs into out_dir."""
    if not transcript_path and not timeline_path:
        raise ValueError("At least one of transcript_path or timeline_path must be provided.")

    if video_dur is None:
        if video_path is None:
            raise ValueError("Either video_dur or video_path must be provided to determine video duration.")
        video_dur = probe_video_duration(video_path, ffmpeg_dir=ffmpeg_dir)

    target_dir = Path(out_dir) if out_dir else Path.cwd()
    stats = ProcessStats(video_duration=video_dur)

    srt_path = None
    if transcript_path is not None:
        paras, skipped_t = parse_transcript(read_lines(transcript_path, "Transcript"))
        srt_content = generate_srt(paras, video_dur=video_dur)
        srt_path = target_dir / f"{base_name}.srt"
        write_text(srt_path, srt_content)

        stats.paragraphs = len(paras)
        stats.skipped_transcript_lines = skipped_t
        stats.srt_cues = srt_content.count(' --> ')
        for sp, _, _ in paras:
            stats.speakers[sp] = stats.speakers.get(sp, 0) + 1

    meta_path = None
    if timeline_path is not None:
        chapters, skipped_c = parse_timeline(read_lines(timeline_path, "Timeline"))
        meta_content = generate_ffmetadata(chapters, video_dur=video_dur)
        meta_path = target_dir / f"{base_name}-chapters.ffmeta"
        write_text(meta_path, meta_content)

        stats.chapters = meta_content.count('[CHAPTER]')
        stats.skipped_timeline_lines = skipped_c

    return srt_path, meta_path, stats


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    parser = argparse.ArgumentParser(description="Convert transcript to SRT and timeline to FFmpeg chapters.")
    parser.add_argument("-t", "--transcript", help="Path to transcript text/md file.")
    parser.add_argument("-l", "--timeline", help="Path to timeline text/md file.")
    parser.add_argument("-v", "--video", help="Path to source video file to probe exact duration.")
    parser.add_argument("-d", "--duration", type=float, default=None, help="Video total duration in seconds.")
    parser.add_argument("-o", "--output-dir", default="./output", help="Directory to save output files.")
    parser.add_argument("-n", "--name", default="output", help="Base name for output files (default: output).")
    parser.add_argument("--ffmpeg-dir", default=None, help="Optional directory containing ffmpeg/ffprobe binaries.")
    args = parser.parse_args()

    if not args.transcript and not args.timeline:
        parser.error("At least one of --transcript or --timeline must be provided.")
    if args.duration is None and args.video is None:
        parser.error("Either --duration or --video must be provided.")

    srt_p, meta_p, stats = process_all(
        transcript_path=args.transcript,
        timeline_path=args.timeline,
        video_dur=args.duration,
        video_path=args.video,
        out_dir=args.output_dir,
        base_name=args.name,
        ffmpeg_dir=args.ffmpeg_dir,
    )

    print(f"video duration: {stats.video_duration:.2f}s")
    if srt_p:
        print(f"paragraphs: {stats.paragraphs}, speakers: {stats.speakers}")
        if stats.skipped_transcript_lines:
            print(f"skipped transcript lines ({len(stats.skipped_transcript_lines)}): {stats.skipped_transcript_lines[:5]}")
        print(f"[OK] SRT cues: {stats.srt_cues} -> {srt_p}")
    if meta_p:
        if stats.skipped_timeline_lines:
            print(f"skipped timeline lines ({len(stats.skipped_timeline_lines)}): {stats.skipped_timeline_lines[:5]}")
        print(f"[OK] Chapters: {stats.chapters} -> {meta_p}")


if __name__ == "__main__":
    main()

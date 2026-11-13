"""Media binary discovery and video duration probing."""

from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess
from pathlib import Path
from typing import BinaryIO, Iterator, Optional, Tuple


def find_binary(name: str, ffmpeg_dir: Optional[str | Path] = None) -> Optional[str]:
    """Locate an executable (e.g. ffmpeg / ffprobe) via explicit dir, FFMPEG_DIR env, or PATH."""
    candidates = []
    if ffmpeg_dir:
        d = Path(ffmpeg_dir)
        candidates.extend([d, d / "bin"])
    env_dir = os.environ.get("FFMPEG_DIR")
    if env_dir:
        ed = Path(env_dir)
        candidates.extend([ed, ed / "bin"])

    exts = (".exe", "") if os.name == "nt" else ("",)
    for directory in candidates:
        for ext in exts:
            candidate = directory / f"{name}{ext}"
            if candidate.is_file():
                return str(candidate)

    return shutil.which(name)


def _iter_mp4_boxes(f: BinaryIO, start: int, end: int) -> Iterator[Tuple[bytes, int, int]]:
    """Yield (box_type, payload_start, box_end) for MP4 boxes within [start, end)."""
    pos = start
    while pos + 8 <= end:
        f.seek(pos)
        hdr = f.read(8)
        if len(hdr) < 8:
            break
        sz, typ = struct.unpack('>I4s', hdr)
        hsz = 8
        if sz == 1:
            ext = f.read(8)
            if len(ext) < 8:
                break
            sz = struct.unpack('>Q', ext)[0]
            hsz = 16
        elif sz == 0:
            sz = end - pos
        if sz < hsz:
            break
        yield typ, pos + hsz, min(pos + sz, end)
        pos += sz


def _probe_mp4_mvhd_duration(mp4_path: Path) -> Optional[float]:
    """Read duration in seconds from an MP4 file's moov/mvhd box without ffprobe."""
    size = mp4_path.stat().st_size
    with open(mp4_path, 'rb') as f:
        for typ, a, b in _iter_mp4_boxes(f, 0, size):
            if typ != b'moov':
                continue
            for mtyp, ma, _ in _iter_mp4_boxes(f, a, b):
                if mtyp != b'mvhd':
                    continue
                f.seek(ma)
                ver = struct.unpack('>B', f.read(1))[0]
                f.read(3)  # flags
                if ver == 0:
                    _, _, timescale, dur = struct.unpack('>IIII', f.read(16))
                else:
                    _, _, timescale, dur = struct.unpack('>QQII', f.read(24))
                return dur / timescale if timescale > 0 else None
    return None


def probe_video_duration(
    video_path: str | Path,
    ffmpeg_dir: Optional[str | Path] = None,
) -> float:
    """Get video duration in seconds via ffprobe (if available) or native MP4 mvhd parsing."""
    path = Path(video_path)
    if not path.is_file():
        raise FileNotFoundError(f"Video file not found: {path}")

    ffprobe_bin = find_binary("ffprobe", ffmpeg_dir=ffmpeg_dir)
    if ffprobe_bin:
        res = subprocess.run(
            [ffprobe_bin, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
        )
        if res.returncode == 0:
            try:
                dur = float(json.loads(res.stdout)["format"]["duration"])
                if dur > 0:
                    return dur
            except (KeyError, ValueError, TypeError, json.JSONDecodeError):
                pass

    if path.suffix.lower() in {".mp4", ".m4v", ".mov"}:
        dur = _probe_mp4_mvhd_duration(path)
        if dur is not None and dur > 0:
            return dur

    raise RuntimeError(
        f"Unable to determine duration for '{path}'. "
        "Install ffprobe, specify --ffmpeg-dir, or pass --duration explicitly."
    )

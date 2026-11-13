"""Shared text and file I/O helpers."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import List, Optional


def ensure_utf8_stdio() -> None:
    """Reconfigure stdout/stderr to UTF-8 with safe fallback to prevent cp1252 crashes on Windows."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


def parse_hms_to_sec(h: Optional[str], m: str, s: str) -> float:
    """Convert hours (optional), minutes, and seconds strings to total seconds."""
    return float((int(h) if h is not None else 0) * 3600 + int(m) * 60 + int(s))


def normalize_lines(text_or_lines: str | List[str]) -> List[str]:
    """Normalize input text or lines, stripping leading UTF-8 BOM and whitespace."""
    raw_lines = text_or_lines.splitlines() if isinstance(text_or_lines, str) else list(text_or_lines)
    return [
        (line.lstrip('\ufeff') if idx == 0 else line).strip()
        for idx, line in enumerate(raw_lines)
    ]


def read_lines(path: str | Path, label: str) -> List[str]:
    """Read text file lines using UTF-8-SIG, raising FileNotFoundError if missing."""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"{label} file not found: {p}")
    with open(p, 'r', encoding='utf-8-sig') as f:
        return f.readlines()


def write_text(path: str | Path, content: str) -> None:
    """Ensure parent directory exists and write UTF-8 text."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding='utf-8')

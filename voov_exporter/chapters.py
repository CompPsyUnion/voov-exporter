"""Parse Tencent Meeting timelines and generate FFMETADATA chapter files."""

from __future__ import annotations

import logging
import re
from typing import List, Tuple

from ._utils import normalize_lines, parse_hms_to_sec

logger = logging.getLogger(__name__)

MAX_TITLE_LEN = 32
CH_RE = re.compile(r'^(?:(\d{1,2}):)?(\d{1,2}):(\d{2})\s+(.*)$')

Chapter = Tuple[float, str]  # (start_time_sec, title)


def _clean_chapter_title(summary: str) -> str:
    """Extract clean chapter title from timeline summary (stripping Markdown bold markers)."""
    cleaned = summary.replace('**', '').replace('__', '').strip()
    title = cleaned.split('。')[0].strip()
    return title[:MAX_TITLE_LEN - 1] + '…' if len(title) > MAX_TITLE_LEN else title


def parse_timeline(text_or_lines: str | List[str]) -> Tuple[List[Chapter], List[str]]:
    """Parse timeline text into (chapters, skipped_lines)."""
    chapters: List[Chapter] = []
    skipped: List[str] = []

    for line in normalize_lines(text_or_lines):
        if not line:
            continue
        m = CH_RE.match(line)
        if not m:
            skipped.append(line)
            continue
        t = parse_hms_to_sec(m.group(1), m.group(2), m.group(3))
        title = _clean_chapter_title(m.group(4).strip())
        if title:
            chapters.append((t, title))
        else:
            skipped.append(line)

    return chapters, skipped


def generate_ffmetadata(chapters: List[Chapter], video_dur: float) -> str:
    """Generate FFMETADATA format string for FFmpeg chapter markers."""
    if video_dur <= 0:
        raise ValueError(f"video_dur must be positive, got {video_dur}")
    if not chapters:
        raise ValueError("No valid chapters found to generate FFMETADATA.")

    valid_chapters = []
    for t, title in chapters:
        if t >= video_dur:
            logger.warning("Skipping chapter '%s' at %.2fs (>= video duration %.2fs)", title, t, video_dur)
            continue
        valid_chapters.append((t, title))

    if not valid_chapters:
        raise ValueError("All timeline chapters fall outside the video duration.")

    meta = [';FFMETADATA1']
    for i, (t, title) in enumerate(valid_chapters):
        end = valid_chapters[i + 1][0] if i + 1 < len(valid_chapters) else video_dur
        if end < t:
            raise ValueError(
                f"Chapter timestamps are not non-decreasing: chapter {i + 1} starts at {t}s, "
                f"next chapter starts at {end}s."
            )
        if end == t:
            logger.warning("Chapter %d ('%s') has zero duration at %.2fs; clamping end by +1.0s", i + 1, title, t)
            end = min(t + 1.0, video_dur)

        meta.extend([
            '[CHAPTER]',
            'TIMEBASE=1/1000',
            f'START={int(round(t * 1000))}',
            f'END={int(round(min(end, video_dur) * 1000))}',
            f'title={i + 1:02d} {title}',
            '',
        ])

    return '\n'.join(meta)

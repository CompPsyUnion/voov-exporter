"""Parse Tencent Meeting transcripts and generate non-overlapping SRT subtitles."""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

from ._utils import normalize_lines, parse_hms_to_sec

DEFAULT_MAX_CUE = 44   # max visible chars per subtitle cue
DEFAULT_MIN_CUE = 18   # merge short clauses up to this many chars
MIN_CUE_DURATION = 0.9
PUNCT = '，。！？；：…、,.!?;:'

# Matches speaker and timestamp at start of line:
PARA_RE = re.compile(r'^(.+?)\s*[（\(](?:(\d{1,2}):)?(\d{1,2}):(\d{2})[）\)]\s*[:：]?\s*(.*)$')

Paragraph = Tuple[str, float, str]  # (speaker, start_time_sec, text)


def nlen(s: str) -> int:
    """Return character count excluding whitespace."""
    return len(re.sub(r'\s+', '', s))


def fmt_srt(t: float) -> str:
    """Format float seconds into SRT timestamp string: HH:MM:SS,mmm."""
    ms = int(round(max(0.0, t) * 1000))
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, ms = divmod(rem, 1000)
    return f'{h:02d}:{m:02d}:{s:02d},{ms:03d}'


def split_clauses(text: str) -> List[str]:
    """Split text into clauses at punctuation marks."""
    clauses, cur = [], ''
    for ch in text:
        cur += ch
        if ch in PUNCT:
            clauses.append(cur)
            cur = ''
    if cur.strip():
        clauses.append(cur)
    return [c for c in clauses if c.strip()]


def hard_split(cl: str, max_cue: int = DEFAULT_MAX_CUE) -> List[str]:
    """Force split long clauses exceeding max_cue visible characters."""
    out = []
    while nlen(cl) > max_cue:
        seg = cl[:max_cue]
        sp = seg.rfind(' ')
        if sp < max_cue * 0.5:
            sp = max_cue - 2
        out.append(cl[:sp + 1])
        cl = cl[sp + 1:]
    if cl.strip():
        out.append(cl)
    return out


def chunk_text(text: str, max_cue: int = DEFAULT_MAX_CUE, min_cue: int = DEFAULT_MIN_CUE) -> List[str]:
    """Chunk a paragraph of text into comfortably readable cue-sized pieces."""
    pieces = [seg for cl in split_clauses(text) for seg in hard_split(cl, max_cue=max_cue)]

    cues, cur = [], ''
    for p in pieces:
        if not cur:
            cur = p
        elif (nlen(cur) < min_cue or nlen(p) <= 6) and nlen(cur) + nlen(p) <= max_cue:
            cur += p
        else:
            cues.append(cur)
            cur = p
    if cur:
        cues.append(cur)
    return cues


def parse_transcript(text_or_lines: str | List[str]) -> Tuple[List[Paragraph], List[str]]:
    """Parse transcript (.md two-line blocks or .txt inline lines) into (paragraphs, skipped_lines)."""
    paras: List[Paragraph] = []
    skipped: List[str] = []
    pending: Optional[Tuple[str, float, List[str]]] = None

    def flush_pending() -> None:
        nonlocal pending
        if pending is not None:
            sp, sec, parts = pending
            body = ' '.join(parts).strip()
            if body:
                paras.append((sp, sec, body))
            pending = None

    for line in normalize_lines(text_or_lines):
        if not line:
            flush_pending()
            continue

        m = PARA_RE.match(line)
        if m:
            flush_pending()
            sp = m.group(1).strip()
            sec = parse_hms_to_sec(m.group(2), m.group(3), m.group(4))
            inline_text = m.group(5).strip()
            if inline_text:
                paras.append((sp, sec, inline_text))
            else:
                pending = (sp, sec, [])
            continue

        if pending is not None:
            pending[2].append(line)
        else:
            skipped.append(line)

    flush_pending()
    return paras, skipped


def generate_srt(
    paras: List[Paragraph],
    video_dur: float,
    max_cue: int = DEFAULT_MAX_CUE,
    min_cue: int = DEFAULT_MIN_CUE,
) -> str:
    """Generate non-overlapping SRT formatted subtitle text from parsed paragraphs."""
    if video_dur <= 0:
        raise ValueError(f"video_dur must be positive, got {video_dur}")
    if not paras:
        raise ValueError("No valid transcript paragraphs found to generate SRT.")

    clamped = [p for p in paras if p[1] < video_dur - 0.5]
    if not clamped:
        raise ValueError("All transcript paragraphs exceed the video duration.")

    raw_cues: List[Tuple[float, float, str]] = []
    prev_sp = None

    for i, (sp, t0, text) in enumerate(clamped):
        t1 = clamped[i + 1][1] if i + 1 < len(clamped) else video_dur
        if t1 <= t0:
            t1 = t0 + 0.8

        cues = chunk_text(text, max_cue=max_cue, min_cue=min_cue)
        total = sum(nlen(c) for c in cues) or 1

        short = sp.split('-', 1)[1] if '-' in sp else sp
        prefix = f'{short}: ' if sp != prev_sp else ''
        prev_sp = sp

        acc = 0
        for c in cues:
            cs = t0 + (t1 - t0) * acc / total
            acc += nlen(c)
            ce = t0 + (t1 - t0) * acc / total
            if ce - cs < MIN_CUE_DURATION:
                ce = cs + MIN_CUE_DURATION
            cue_text = (prefix + c).strip() if prefix else c.strip()
            raw_cues.append((cs, min(ce, video_dur), cue_text))
            prefix = ''

    # Clamp end times so consecutive cues never overlap
    srt_lines: List[str] = []
    for idx, (cs, ce, cue_text) in enumerate(raw_cues, start=1):
        if idx < len(raw_cues):
            next_cs = raw_cues[idx][0]
            if next_cs > cs and ce > next_cs:
                ce = next_cs
        srt_lines.extend([str(idx), f'{fmt_srt(cs)} --> {fmt_srt(ce)}', cue_text, ''])

    return '\n'.join(srt_lines)

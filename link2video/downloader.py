"""Download Tencent Meeting cloud recordings, transcripts, and timeline chapters."""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from ._utils import ensure_utf8_stdio, write_text

logger = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_BASE_HOST = "https://meeting.tencent.com"

TRACK_PRIORITY: Dict[str, Tuple[int, ...]] = {
    "composite": (0, 1, 2),
    "screen": (1, 0, 2),
    "speaker": (2, 0, 1),
    "audio": (3,),
}

_PASSWORD_ERROR_CODES = {10405, 10200, 10201, 10202, 10203, 1511}
_TRANSCRIPT_UNAVAILABLE_CODES = {30003, 30015, 30016, 30017, 30020}


@dataclass
class MeetingMeta:
    """Resolved identifiers and metadata for a Tencent Meeting recording link."""
    url: str
    short_code: str
    share_id: str
    meeting_id: str
    record_id: str
    title: str
    pwd: str = ""
    is_single: bool = False
    raw_recording: Optional[Dict[str, Any]] = None


@dataclass
class DownloadResult:
    """Paths and metadata produced by downloading a Tencent Meeting recording."""
    title: str
    video_path: Path
    transcript_path: Optional[Path]
    timeline_path: Optional[Path]
    meta: MeetingMeta


def _fmt_hms(seconds: float) -> str:
    sec = max(0, int(round(seconds)))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _decode_subject(raw: str) -> str:
    if not raw:
        return "tencent_meeting"
    try:
        decoded = base64.b64decode(raw, validate=True).decode("utf-8")
        if decoded and all(ch.isprintable() or ch.isspace() for ch in decoded):
            return decoded.strip()
    except Exception:
        pass
    return raw.strip()


def _sanitize_filename(name: str, fallback: str = "tencent_meeting") -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", name).strip(" .")
    return cleaned[:100] or fallback


def _is_password_error(code: Optional[int], msg: str, err_detail: str) -> bool:
    if code in _PASSWORD_ERROR_CODES:
        return True
    combined = f"{msg} {err_detail}".lower()
    return any(tok in combined for tok in ("password", "invalid pwd", "pwd", "密码"))


class _HttpSession:
    """Thin HTTP JSON/HTML/stream client using Python stdlib urllib."""

    def __init__(self, cookie: Optional[str] = None, timeout: int = 25, retries: int = 3) -> None:
        self.cookie = cookie
        self.timeout = timeout
        self.retries = retries

    def _headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        hdrs = {
            "User-Agent": _UA,
            "Referer": f"{_BASE_HOST}/",
        }
        if self.cookie:
            hdrs["Cookie"] = self.cookie
        if extra:
            hdrs.update(extra)
        return hdrs

    def _request_bytes(
        self,
        url: str,
        method: str = "GET",
        data: Optional[bytes] = None,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> Tuple[str, bytes]:
        last_exc: Optional[Exception] = None
        for attempt in range(self.retries):
            req = urllib.request.Request(url, data=data, headers=self._headers(extra_headers), method=method)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return resp.geturl(), resp.read()
            except urllib.error.HTTPError:
                raise
            except (TimeoutError, urllib.error.URLError, OSError) as exc:
                last_exc = exc
                if attempt + 1 < self.retries:
                    time.sleep(0.5 * (attempt + 1))
        raise RuntimeError(f"HTTP request failed after {self.retries} attempts ({url}): {last_exc}") from last_exc

    def get_text(self, url: str) -> Tuple[str, str]:
        final_url, raw = self._request_bytes(url, method="GET")
        return final_url, raw.decode("utf-8", "replace")

    def get_json(self, url: str) -> Dict[str, Any]:
        _, raw = self._request_bytes(url, method="GET")
        return json.loads(raw.decode("utf-8", "replace"))

    def post_json(self, url: str, body: Dict[str, Any]) -> Dict[str, Any]:
        payload = json.dumps(body).encode("utf-8")
        _, raw = self._request_bytes(
            url, method="POST", data=payload, extra_headers={"Content-Type": "application/json"},
        )
        return json.loads(raw.decode("utf-8", "replace"))

    def download_stream(self, url: str, dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_suffix(dest.suffix + ".part")
        last_exc: Optional[Exception] = None
        for attempt in range(self.retries):
            try:
                existing = part.stat().st_size if part.is_file() else 0
                extra = {"Range": f"bytes={existing}-"} if existing > 0 else None
                req = urllib.request.Request(url, headers=self._headers(extra), method="GET")
                with urllib.request.urlopen(req, timeout=60) as resp:
                    if existing > 0 and resp.status == 206:
                        mode = "ab"
                        downloaded = existing
                    else:
                        mode = "wb"
                        downloaded = 0
                    total_hdr = resp.headers.get("Content-Length")
                    total = (downloaded + int(total_hdr)) if (total_hdr and total_hdr.isdigit()) else 0
                    next_report = 0
                    with open(part, mode) as fh:
                        while True:
                            chunk = resp.read(256 * 1024)
                            if not chunk:
                                break
                            fh.write(chunk)
                            downloaded += len(chunk)
                            if total > 0:
                                pct = int(downloaded * 100 / total)
                                if pct >= next_report:
                                    print(
                                        f"\rDownloading video: {pct:3d}% ({downloaded // (1024 * 1024)} / {total // (1024 * 1024)} MB)",
                                        end="",
                                        flush=True,
                                    )
                                    next_report = pct + 10
                            elif downloaded >= next_report:
                                print(f"\rDownloading video: {downloaded // (1024 * 1024)} MB", end="", flush=True)
                                next_report = downloaded + 10 * 1024 * 1024
                if downloaded > 0:
                    print(flush=True)
                os.replace(part, dest)
                return dest
            except urllib.error.HTTPError:
                raise
            except (TimeoutError, urllib.error.URLError, OSError) as exc:
                last_exc = exc
                if attempt + 1 < self.retries:
                    time.sleep(0.5 * (attempt + 1))
        raise RuntimeError(f"Video download failed after {self.retries} attempts: {last_exc}") from last_exc


def _extract_js_redirect(html: str) -> Optional[str]:
    m = re.search(r'window\.location\.replace\(\s*["\']([^"\']+)["\']\s*\)', html)
    return m.group(1) if m else None


def _extract_rsc_payload(html: str) -> Dict[str, Any]:
    """Extract embedded Next.js App Router serverData/query payload from SSR HTML."""
    for raw_chunk in re.findall(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', html, re.S):
        try:
            text = json.loads('"' + raw_chunk + '"')
        except Exception:
            text = raw_chunk
        if '"serverData"' not in text:
            continue
        idx = text.find("{")
        if idx == -1:
            continue
        try:
            obj, _ = json.JSONDecoder().raw_decode(text[idx:])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            if "serverData" in obj:
                return obj
            data = obj.get("data")
            if isinstance(data, dict) and "serverData" in data:
                return data
    return {}


def _parse_url_identifiers(url: str, rsc: Dict[str, Any]) -> Tuple[str, str, bool]:
    """Return (short_code, share_id, is_single) from URL + RSC payload."""
    parsed = urllib.parse.urlparse(url)
    qs = urllib.parse.parse_qs(parsed.query)
    share_id = qs.get("id", [""])[0]
    is_single = qs.get("is-single", ["false"])[0].lower() == "true"

    short_code = rsc.get("shortLink", "")
    if not short_code:
        m = re.search(r"/(?:crm|cw)/([A-Za-z0-9_-]+)", parsed.path)
        if m:
            short_code = m.group(1)

    server_data = rsc.get("serverData") or {}
    long_url = (server_data.get("short_url_info") or {}).get("long_url", "") or rsc.get("fullLongUrlSearch", "")
    if long_url:
        lqs = urllib.parse.parse_qs(long_url.lstrip("?"))
        if not share_id:
            share_id = lqs.get("id", [""])[0]
        if "is-single" in lqs:
            is_single = lqs["is-single"][0].lower() == "true"

    if not share_id:
        share_id = server_data.get("share_id") or server_data.get("sharing_id") or ""
    if not share_id:
        recs = server_data.get("recordings") or []
        if recs and isinstance(recs[0], dict):
            share_id = recs[0].get("sharing_id", "")

    return short_code, share_id, is_single


def _webtapi_qs(extra: Optional[Dict[str, Any]] = None) -> str:
    params: Dict[str, Any] = {
        "c_app_id": "1400143280",
        "c_os_model": "web",
        "c_os": "web",
        "c_os_version": "1",
        "c_timestamp": str(int(time.time() * 1000)),
        "c_nonce": "l2v00001",
        "c_app_version": "1",
        "c_instance_id": "5",
        "rnds": "l2v00001",
        "c_district": "0",
        "platform": "Web",
        "c_lang": "zh-CN",
    }
    if extra:
        params.update(extra)
    return urllib.parse.urlencode(params)


def _prompt_for_password(
    prompt_fn: Callable[[str], str],
    message: str,
) -> str:
    try:
        entered = prompt_fn(message).strip()
    except (EOFError, KeyboardInterrupt) as exc:
        raise PermissionError("Access password required, but input was aborted.") from exc
    if not entered:
        raise PermissionError("No access password entered; download cancelled.")
    return entered


def resolve_meeting_meta(
    url: str,
    http: _HttpSession,
    password: Optional[str] = None,
    password_prompt: Callable[[str], str] = input,
    max_pwd_attempts: int = 3,
) -> MeetingMeta:
    """Resolve meeting metadata and handle access passwords interactively if required."""
    final_url, html = http.get_text(url)
    redir = _extract_js_redirect(html)
    if redir:
        final_url, html = http.get_text(urllib.parse.urljoin(final_url, redir))

    rsc = _extract_rsc_payload(html)
    server_data = rsc.get("serverData") or {}
    short_code, share_id, is_single = _parse_url_identifiers(final_url, rsc)

    if not share_id and not server_data.get("recordings"):
        raise RuntimeError(
            f"Failed to resolve recording identifiers from {final_url}. "
            "The link may be expired, require login (pass browser cookies via --cookie), or be gated by a CAPTCHA."
        )

    shares_ep = "v1/recording/shares" if is_single else "v1/shares"
    shares_url = f"{_BASE_HOST}/wemeet-cloudrecording-webapi/{shares_ep}?c_os_model=web&c_os=web&platform=Web"

    pwd = password or ""
    share_res: Optional[Dict[str, Any]] = None
    attempts = 0
    while True:
        payload = {
            "id": share_id,
            "pwd": pwd,
            "short_url_code": short_code,
            "lang": "zh",
            "enter_from": "share",
        }
        share_res = http.post_json(shares_url, payload)
        code = share_res.get("code")
        if code == 0 and (share_res.get("recordings") or is_single):
            break

        msg = str(share_res.get("msg") or "")
        err_detail = str(share_res.get("err_detail") or "")
        # Fallback to SSR serverData only when link is public and no password issue is indicated.
        if (
            not pwd
            and server_data.get("code") == 0
            and server_data.get("recordings")
            and not _is_password_error(code, msg, err_detail)
        ):
            share_res = server_data
            break

        needs_pwd = _is_password_error(code, msg, err_detail) or (
            server_data.get("code") in _PASSWORD_ERROR_CODES
        )
        if not needs_pwd:
            raise RuntimeError(f"Failed to fetch meeting metadata (code={code}): {err_detail or msg or share_res}")

        if attempts >= max_pwd_attempts:
            raise PermissionError("Too many incorrect password attempts; download aborted.")

        prompt_msg = (
            "This Tencent Meeting link requires an access password. Enter password (press Enter to cancel): "
            if attempts == 0 and not pwd
            else "Incorrect password. Please re-enter password (press Enter to cancel): "
        )
        pwd = _prompt_for_password(password_prompt, prompt_msg)
        attempts += 1

    recs = share_res.get("recordings") or server_data.get("recordings") or []
    if not recs:
        raise RuntimeError("No downloadable cloud recording segments found (recordings list is empty).")

    rec0 = recs[0]
    meeting_info = share_res.get("meeting_info") or server_data.get("meeting_info") or {}
    meeting_id = str(
        share_res.get("meeting_id")
        or meeting_info.get("meeting_id")
        or server_data.get("meeting_id")
        or rec0.get("str_meeting_id")
        or ""
    )
    record_id = str(rec0.get("id") or "")
    raw_subject = (
        share_res.get("subject")
        or meeting_info.get("subject")
        or server_data.get("subject")
        or short_code
        or "tencent_meeting"
    )
    verified_pwd = str(share_res.get("pwd") or pwd or "")
    effective_share_id = share_id or str(rec0.get("sharing_id") or "")

    if not record_id:
        raise RuntimeError("Failed to resolve recording_id.")

    return MeetingMeta(
        url=final_url,
        short_code=short_code,
        share_id=effective_share_id,
        meeting_id=meeting_id,
        record_id=record_id,
        title=_decode_subject(str(raw_subject)),
        pwd=verified_pwd,
        is_single=is_single,
        raw_recording=rec0,
    )


def resolve_video_download_url(
    meta: MeetingMeta,
    http: _HttpSession,
    track: str = "composite",
) -> str:
    """Resolve and sign the COS MP4 download URL for the chosen track."""
    q_list = urllib.parse.urlencode({
        "record_id": meta.record_id,
        "auth_share_id": meta.share_id,
        "pwd": meta.pwd,
        "activity_uid": "",
        "support_ai_pro": "1",
    })
    files_url = f"{_BASE_HOST}/wemeet-tapi/v2/meetlog/public/record-detail/get-multi-record-file?{q_list}"
    files_res = http.get_json(files_url)
    files = (files_res.get("data") or {}).get("files") or []

    priorities = TRACK_PRIORITY.get(track, TRACK_PRIORITY["composite"])
    chosen: Optional[Dict[str, Any]] = None
    for rtype in priorities:
        matches = [f for f in files if f.get("resource_type") == rtype]
        if matches:
            non_empty = [f for f in matches if int(f.get("size") or 0) > 0]
            chosen = non_empty[0] if non_empty else matches[0]
            break

    if chosen is not None:
        q_sign = urllib.parse.urlencode({
            "record_id": meta.record_id,
            "resource_id": chosen.get("resource_id", meta.record_id),
            "resource_type": chosen.get("resource_type", 0),
            "auth_share_id": meta.share_id,
            "pwd": meta.pwd,
        })
        sign_url = f"{_BASE_HOST}/wemeet-tapi/v2/meetlog/public/record-detail/sign-multi-record-file?{q_sign}"
        sign_res = http.get_json(sign_url)
        signed = (sign_res.get("data") or {}).get("url")
        if signed:
            return signed
        raise RuntimeError(
            f"Failed to sign video download URL (code={sign_res.get('code')}): "
            f"{sign_res.get('err_detail') or sign_res.get('msg') or sign_res}"
        )

    rec = meta.raw_recording or {}
    fallback_url = rec.get("download_address") or rec.get("view_address") or ""
    if fallback_url:
        return fallback_url

    raise RuntimeError(
        f"No downloadable video track found (code={files_res.get('code')}): "
        f"{files_res.get('err_detail') or files_res.get('msg') or 'video file missing'}"
    )


def minutes_json_to_transcript(paragraphs: List[Dict[str, Any]]) -> Optional[str]:
    """Convert minutes/detail JSON paragraphs into standard markdown transcript format."""
    blocks: List[str] = []
    for p in paragraphs:
        speaker = ((p.get("speaker") or {}).get("user_name") or "发言人").strip()
        start_ms = int(p.get("start_time") or 0)
        hms = _fmt_hms(start_ms / 1000.0)
        parts: List[str] = []
        for sent in p.get("sentences") or []:
            for word in sent.get("words") or []:
                parts.append(word.get("text", ""))
        body = "".join(parts).strip()
        if body:
            blocks.append(f"{speaker}({hms}):\n{body}\n")
    return "\n".join(blocks) + "\n" if blocks else None


def fetch_transcript_text(meta: MeetingMeta, http: _HttpSession) -> Optional[str]:
    """Fetch paginated/full transcript from minutes/detail and format for Part 1."""
    all_paras: List[Dict[str, Any]] = []
    start_pid: Optional[str] = None

    while True:
        params: Dict[str, Any] = {
            "c_os_model": "web",
            "c_os": "web",
            "platform": "Web",
            "id": meta.share_id,
            "pwd": meta.pwd,
            "page_source": "record",
            "meeting_id": meta.meeting_id,
            "recording_id": meta.record_id,
            "lang": "zh",
            "minutes_version": "0",
            "return_ori": "0",
            "return_ori_minutes_translating": "1",
            "split_version": "1",
            "fview": "0",
            "support_ai_pro": "1",
            "enter_from": "share",
        }
        if start_pid is not None:
            params["start_pid"] = start_pid
            params["limit"] = "200"

        url = f"{_BASE_HOST}/wemeet-cloudrecording-webapi/v1/minutes/detail?{urllib.parse.urlencode(params)}"
        res = http.get_json(url)
        code = res.get("code")
        if code != 0:
            if code in _TRANSCRIPT_UNAVAILABLE_CODES:
                logger.warning("Transcript unavailable (code=%s: %s)", code, res.get("msg") or "")
                return None
            logger.warning("Failed to fetch transcript (code=%s): %s", code, res.get("err_detail") or res.get("msg"))
            return None

        paras = (res.get("minutes") or {}).get("paragraphs") or []
        if not paras:
            break
        all_paras.extend(paras)
        if not res.get("more"):
            break
        next_pid = str(paras[-1].get("pid", len(all_paras)))
        if next_pid == start_pid:
            break
        start_pid = next_pid

    return minutes_json_to_transcript(all_paras)


def summary_infos_to_timeline(summary_infos: List[Dict[str, Any]]) -> Optional[str]:
    """Convert official_template_summary items with time_list into timeline format."""
    entries: List[Tuple[int, str]] = []
    for item in summary_infos:
        tlist = item.get("time_list") or []
        if not tlist:
            continue
        try:
            t_sec = min(int(t) for t in tlist)
        except (TypeError, ValueError):
            continue
        raw_content = str(item.get("content") or "").strip()
        raw_content = re.sub(r"^\s*[-*•]\s*", "", raw_content)
        # Convert "**Title**：Desc" -> "**Title。**Desc" so chapters.py extracts Title cleanly
        m = re.match(r"^\*\*(.+?)\*\*\s*[:：]\s*(.*)$", raw_content, re.S)
        if m:
            title = m.group(1).strip().rstrip("。")
            desc = m.group(2).strip().replace("\n", " ")
            line_body = f"**{title}。**{desc}"
        else:
            line_body = raw_content.replace("\n", " ")
        if line_body:
            entries.append((t_sec, line_body))

    if not entries:
        return None

    entries.sort(key=lambda x: x[0])
    seen_ts = set()
    lines: List[str] = []
    for t_sec, body in entries:
        if t_sec in seen_ts:
            continue
        seen_ts.add(t_sec)
        lines.append(f"{_fmt_hms(t_sec)} {body}")
    return "\n".join(lines) + "\n" if lines else None


def fetch_timeline_text(meta: MeetingMeta, http: _HttpSession) -> Optional[str]:
    """Fetch timeline/chapter markers via chapter API or summary time_list fallback."""
    qs = _webtapi_qs({"meeting_id": meta.meeting_id, "support_ai_pro": "1"})

    # 1. Try explicit chapter / smart-topic endpoints first
    for ep, key, time_key, title_key in (
        ("get-chapter", "chapters", "start_time", "title"),
        ("get-time-line", "timelines", "start_time", "summary"),
    ):
        body = {
            "meeting_id": meta.meeting_id,
            "record_id": meta.record_id,
            "share_id": meta.share_id,
            "pwd": meta.pwd,
            "activity_uid": "",
            "lang": "zh",
            "page": 1,
            "page_size": 100,
            "support_ai_pro": 1,
        }
        try:
            res = http.post_json(f"{_BASE_HOST}/wemeet-tapi/v2/meetlog/public/record-detail/{ep}?{qs}", body)
        except Exception as exc:
            logger.debug("Timeline endpoint %s failed: %s", ep, exc)
            continue
        if res.get("code") == 0:
            items = (res.get("data") or {}).get(key) or []
            parsed: List[Tuple[int, str]] = []
            for it in items:
                if time_key not in it:
                    continue
                raw_t = float(it[time_key])
                t_sec = int(round(raw_t / 1000.0)) if raw_t > 86400 else int(round(raw_t))
                txt = str(it.get(title_key) or it.get("content") or it.get("title") or "").strip()
                if txt:
                    parsed.append((t_sec, txt.replace("\n", " ")))
            if parsed:
                parsed.sort(key=lambda x: x[0])
                return "\n".join(f"{_fmt_hms(t)} **{txt}**" for t, txt in parsed) + "\n"

    # 2. Fallback: structured official_template_summary / template_summary with time_list
    try:
        qsn = http.post_json(
            f"{_BASE_HOST}/wemeet-tapi/v2/meetlog/public/record-detail/query-summary-and-note?{qs}",
            {
                "meeting_id": meta.meeting_id,
                "record_id": meta.record_id,
                "share_id": meta.share_id,
                "pwd": meta.pwd,
                "activity_uid": "",
                "lang": "zh",
                "support_ai_pro": 1,
            },
        )
    except Exception as exc:
        logger.warning("Failed to fetch summary/timeline: %s", exc)
        return None

    if qsn.get("code") != 0:
        logger.warning("Summary/timeline unavailable (code=%s): %s", qsn.get("code"), qsn.get("err_detail") or qsn.get("msg"))
        return None

    data = qsn.get("data") or {}
    for summary_key in ("official_template_summary", "template_summary", "deepseek_summary"):
        block = data.get(summary_key)
        if isinstance(block, dict) and block.get("summary_infos"):
            tl = summary_infos_to_timeline(block["summary_infos"])
            if tl:
                return tl

    return None


def download_meeting(
    url: str,
    output_dir: str | Path,
    track: str = "composite",
    password: Optional[str] = None,
    cookie: Optional[str] = None,
    password_prompt: Callable[[str], str] = input,
    http: Optional[_HttpSession] = None,
) -> DownloadResult:
    """Download video MP4, transcript text, and timeline text from a Tencent Meeting URL."""
    session = http or _HttpSession(cookie=cookie)
    meta = resolve_meeting_meta(
        url=url,
        http=session,
        password=password,
        password_prompt=password_prompt,
    )
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_base = _sanitize_filename(meta.title)

    video_url = resolve_video_download_url(meta, session, track=track)
    ext = ".m4a" if track == "audio" else ".mp4"
    video_path = out_dir / f"{safe_base}{ext}"
    logger.info("Downloading video track (%s) -> %s", track, video_path)
    session.download_stream(video_url, video_path)

    transcript_path: Optional[Path] = None
    transcript_text = fetch_transcript_text(meta, session)
    if transcript_text:
        transcript_path = out_dir / f"{safe_base}-transcript.md"
        write_text(transcript_path, transcript_text)

    timeline_path: Optional[Path] = None
    timeline_text = fetch_timeline_text(meta, session)
    if timeline_text:
        timeline_path = out_dir / f"{safe_base}-timeline.md"
        write_text(timeline_path, timeline_text)

    return DownloadResult(
        title=meta.title,
        video_path=video_path,
        transcript_path=transcript_path,
        timeline_path=timeline_path,
        meta=meta,
    )


def main() -> None:
    ensure_utf8_stdio()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    parser = argparse.ArgumentParser(description="Download Tencent Meeting video, transcript, and timeline.")
    parser.add_argument("--url", required=True, help="Tencent Meeting share URL.")
    parser.add_argument("-o", "--output-dir", default="./output/raw", help="Directory to save downloaded assets.")
    parser.add_argument(
        "--track",
        choices=list(TRACK_PRIORITY.keys()),
        default="composite",
        help="Video track preference (default: composite).",
    )
    parser.add_argument("-p", "--password", default=None, help="Meeting access password (prompted if omitted & needed).")
    parser.add_argument("-c", "--cookie", default=None, help="Optional browser Cookie header for login-only links.")
    args = parser.parse_args()

    res = download_meeting(
        url=args.url,
        output_dir=args.output_dir,
        track=args.track,
        password=args.password,
        cookie=args.cookie,
    )
    print(f"[OK] Title: {res.title}")
    print(f"[OK] Video: {res.video_path}")
    print(f"[OK] Transcript: {res.transcript_path or '(unavailable)'}")
    print(f"[OK] Timeline: {res.timeline_path or '(unavailable)'}")


if __name__ == "__main__":
    main()

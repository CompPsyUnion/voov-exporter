import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import patch

from link2video.chapters import generate_ffmetadata, parse_timeline
from link2video.cli import main as cli_main
from link2video.downloader import (
    DownloadResult,
    _extract_js_redirect,
    _extract_rsc_payload,
    minutes_json_to_transcript,
    resolve_meeting_meta,
    resolve_video_download_url,
    summary_infos_to_timeline,
)
from link2video.media import find_binary
from link2video.muxer import build_mux_command, mux_media
from link2video.subtitles import generate_srt, parse_transcript

LOCAL_FFMPEG_DIR = Path(r"D:\ffmpeg-9.0-full_build")


def _get_ffmpeg_and_ffprobe() -> Tuple[Optional[str], Optional[str]]:
    ff = find_binary("ffmpeg", ffmpeg_dir=LOCAL_FFMPEG_DIR if LOCAL_FFMPEG_DIR.is_dir() else None)
    fp = find_binary("ffprobe", ffmpeg_dir=LOCAL_FFMPEG_DIR if LOCAL_FFMPEG_DIR.is_dir() else None)
    return ff, fp


def _make_sample_mp4(ffmpeg_bin: str, dest: Path, duration: int = 5) -> None:
    cmd = [
        ffmpeg_bin, "-y",
        "-f", "lavfi", "-i", f"testsrc=duration={duration}:size=320x180:rate=15",
        "-f", "lavfi", "-i", f"sine=duration={duration}",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
        str(dest),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
    if res.returncode != 0:
        raise RuntimeError(f"Failed to generate sample MP4: {res.stderr[-500:]}")


def _probe_json(ffprobe_bin: str, media_path: Path, show_chapters: bool = False) -> Dict[str, Any]:
    cmd = [ffprobe_bin, "-v", "error", "-show_streams"]
    if show_chapters:
        cmd.append("-show_chapters")
    cmd.extend(["-of", "json", str(media_path)])
    res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=True)
    return json.loads(res.stdout)


class _FakeHttpSession:
    def __init__(self, get_texts: Dict[str, Tuple[str, str]], get_jsons: Dict[str, Any], post_handler):
        self.get_texts = get_texts
        self.get_jsons = get_jsons
        self.post_handler = post_handler
        self.post_calls: List[Tuple[str, Dict[str, Any]]] = []
        self.get_calls: List[str] = []

    def get_text(self, url: str) -> Tuple[str, str]:
        self.get_calls.append(url)
        for prefix, val in self.get_texts.items():
            if url.startswith(prefix):
                return val
        raise AssertionError(f"Unexpected get_text URL: {url}")

    def get_json(self, url: str) -> Dict[str, Any]:
        self.get_calls.append(url)
        for prefix, val in self.get_jsons.items():
            if prefix in url:
                return val
        raise AssertionError(f"Unexpected get_json URL: {url}")

    def post_json(self, url: str, body: Dict[str, Any]) -> Dict[str, Any]:
        self.post_calls.append((url, body))
        return self.post_handler(url, body)


class TestMuxer(unittest.TestCase):
    def test_build_mux_command_variants(self):
        cmd_mp4 = build_mux_command("ffmpeg", Path("in.mp4"), Path("out.mp4"), Path("sub.srt"), Path("ch.ffmeta"))
        self.assertIn("mov_text", cmd_mp4)
        self.assertIn("+faststart", cmd_mp4)
        self.assertEqual(cmd_mp4[cmd_mp4.index("-map_chapters") + 1], "1")
        self.assertIn("2:0", cmd_mp4)

        cmd_mkv = build_mux_command("ffmpeg", Path("in.mp4"), Path("out.mkv"), srt_path=Path("sub.srt"))
        self.assertIn("srt", cmd_mkv)
        self.assertNotIn("-map_chapters", cmd_mkv)
        self.assertIn("1:0", cmd_mkv)

        cmd_burn = build_mux_command(
            "ffmpeg", Path("in.mp4"), Path("out.mp4"), srt_path=Path("sub.srt"), meta_path=Path("ch.ffmeta"), burn_subs=True
        )
        self.assertIn("-vf", cmd_burn)
        self.assertIn("libx264", cmd_burn)
        self.assertNotIn("mov_text", cmd_burn)
        self.assertEqual(cmd_burn[cmd_burn.index("-map_chapters") + 1], "1")

    def test_mux_validation_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            v = p / "v.mp4"
            v.write_bytes(b"fake")
            with self.assertRaises(FileNotFoundError):
                mux_media(p / "missing.mp4", p / "out.mp4", srt_path=p / "s.srt")
            with self.assertRaises(ValueError):
                mux_media(v, p / "out.mp4")

    def test_real_ffmpeg_mux_mp4_and_mkv(self):
        ff, fp = _get_ffmpeg_and_ffprobe()
        if not (ff and fp):
            self.skipTest("ffmpeg/ffprobe binaries not available")

        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            src = p / "sample.mp4"
            _make_sample_mp4(ff, src, duration=5)

            srt = p / "subs.srt"
            srt.write_text("1\n00:00:00,500 --> 00:00:02,500\n测试第一句字幕\n\n2\n00:00:02,600 --> 00:00:04,500\n测试第二句字幕\n", encoding="utf-8")
            meta = p / "chapters.ffmeta"
            meta.write_text(
                ";FFMETADATA1\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=2500\ntitle=01 开场\n\n"
                "[CHAPTER]\nTIMEBASE=1/1000\nSTART=2500\nEND=5000\ntitle=02 收尾\n",
                encoding="utf-8",
            )

            ffdir = Path(ff).parent
            for ext, expected_sub_codec in ((".mp4", "mov_text"), (".mkv", "subrip")):
                out = mux_media(src, p / f"merged{ext}", srt_path=srt, meta_path=meta, ffmpeg_dir=ffdir)
                self.assertTrue(out.is_file())
                info = _probe_json(fp, out, show_chapters=True)
                codecs = [s["codec_name"] for s in info["streams"]]
                self.assertIn(expected_sub_codec, codecs)
                chapters = info["chapters"]
                self.assertEqual(len(chapters), 2)
                self.assertEqual(chapters[0]["tags"]["title"], "01 开场")
                self.assertEqual(chapters[1]["tags"]["title"], "02 收尾")

            burned = mux_media(src, p / "burned.mp4", srt_path=srt, meta_path=meta, ffmpeg_dir=ffdir, burn_subs=True)
            self.assertTrue(burned.is_file())
            info_b = _probe_json(fp, burned, show_chapters=True)
            self.assertNotIn("subtitle", [s["codec_type"] for s in info_b["streams"]])
            self.assertIn("h264", [s["codec_name"] for s in info_b["streams"]])
            self.assertEqual(len(info_b["chapters"]), 2)


class TestDownloader(unittest.TestCase):
    def test_extract_redirect_and_rsc(self):
        html1 = '<script>window.location.replace("https://meeting.tencent.com/cw/AbCdEf123");</script>'
        self.assertEqual(_extract_js_redirect(html1), "https://meeting.tencent.com/cw/AbCdEf123")

        payload = {
            "serverData": {
                "code": 0,
                "meeting_id": "999",
                "short_url_info": {"long_url": "id=share-uuid-1&from=3"},
                "recordings": [{"id": "rec-1"}],
            },
            "shortLink": "AbCdEf123",
        }
        rsc_line = '18:["$","$L19",null,' + json.dumps({"data": payload}) + ']'
        html4 = f'<script>self.__next_f.push([1,{json.dumps(rsc_line)}])</script>'
        rsc = _extract_rsc_payload(html4)
        self.assertEqual(rsc["shortLink"], "AbCdEf123")
        self.assertEqual(rsc["serverData"]["meeting_id"], "999")

    def test_access_failure_prompts_password_or_account(self):
        payload = {
            "serverData": {
                "code": 10405,
                "short_url_info": {"long_url": "id=uuid-lock"},
                "recordings": [],
            },
            "shortLink": "Locked123",
        }
        cw_html = f'<script>self.__next_f.push([1,{json.dumps(json.dumps(payload))}])</script>'

        http = _FakeHttpSession(
            get_texts={"https://meeting.tencent.com/cw/Locked123": ("https://meeting.tencent.com/cw/Locked123", cw_html)},
            get_jsons={},
            post_handler=lambda u, b: {"code": 10405, "msg": "invalid password"},
        )
        with self.assertRaises(PermissionError) as ctx:
            resolve_meeting_meta("https://meeting.tencent.com/cw/Locked123", http)
        msg = str(ctx.exception).lower()
        self.assertIn("password", msg)
        self.assertIn("account login", msg)
        self.assertEqual(len(http.post_calls), 1)

        no_id_html = "<html><body>login required</body></html>"
        http_no_id = _FakeHttpSession(
            get_texts={"https://meeting.tencent.com/cw/Private": ("https://meeting.tencent.com/cw/Private", no_id_html)},
            get_jsons={},
            post_handler=lambda u, b: {},
        )
        with self.assertRaises(PermissionError) as ctx2:
            resolve_meeting_meta("https://meeting.tencent.com/cw/Private", http_no_id)
        msg2 = str(ctx2.exception).lower()
        self.assertIn("password", msg2)
        self.assertIn("account login", msg2)

    def test_video_track_selection_and_converters(self):
        http = _FakeHttpSession(
            get_texts={},
            get_jsons={
                "get-multi-record-file": {
                    "code": 0,
                    "data": {
                        "files": [
                            {"resource_type": 0, "resource_id": "r-1", "size": "100"},
                            {"resource_type": 1, "resource_id": "r-1", "size": "200"},
                        ]
                    },
                },
                "sign-multi-record-file": {"code": 0, "data": {"url": "https://cdn.example/video.mp4?token=abc"}},
            },
            post_handler=lambda u, b: {},
        )
        from link2video.downloader import MeetingMeta
        meta = MeetingMeta("u", "sc", "sh", "m1", "r-1", "Title")
        url = resolve_video_download_url(meta, http, track="screen")
        self.assertEqual(url, "https://cdn.example/video.mp4?token=abc")
        self.assertTrue(any("resource_type=1" in c for c in http.get_calls))

        # Minutes -> transcript -> SRT roundtrip
        tr_text = minutes_json_to_transcript([
            {"start_time": 5000, "speaker": {"user_name": "张三-发言人1"}, "sentences": [{"words": [{"text": "你好，世界。"}]}]},
            {"start_time": 12000, "speaker": {"user_name": "李四-发言人2"}, "sentences": [{"words": [{"text": "开始分享。"}]}]},
        ])
        self.assertIsNotNone(tr_text)
        paras, skipped = parse_transcript(tr_text)
        self.assertEqual(skipped, [])
        srt = generate_srt(paras, video_dur=20.0)
        self.assertIn("发言人1: 你好，世界。", srt)

        # Unsorted summary_infos -> timeline -> FFMETADATA roundtrip
        tl_text = summary_infos_to_timeline([
            {"time_list": [30, 45], "content": "- **第二部分**：深入讲解"},
            {"time_list": [5], "content": "- **第一部分**：背景介绍"},
        ])
        self.assertIsNotNone(tl_text)
        chapters, skipped_c = parse_timeline(tl_text)
        self.assertEqual(skipped_c, [])
        self.assertEqual(chapters[0][0], 5.0)
        self.assertEqual(chapters[1][0], 30.0)
        ffmeta = generate_ffmetadata(chapters, video_dur=60.0)
        self.assertIn("title=01 第一部分", ffmeta)
        self.assertIn("title=02 第二部分", ffmeta)


class TestCLI(unittest.TestCase):
    def test_cli_local_end_to_end(self):
        ff, fp = _get_ffmpeg_and_ffprobe()
        if not (ff and fp):
            self.skipTest("ffmpeg/ffprobe binaries not available")

        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            src = p / "raw.mp4"
            _make_sample_mp4(ff, src, duration=6)
            tr = p / "tr.md"
            tr.write_text("张三(00:00:01):\n大家好，今天演示一下。\n\n李四(00:00:04):\n好的收到。\n", encoding="utf-8")
            tl = p / "tl.md"
            tl.write_text("00:00:00 **开场。**介绍\n00:00:03 **结尾。**总结\n", encoding="utf-8")
            out = p / "final.mp4"

            rc = cli_main([
                "--local",
                "-v", str(src),
                "-t", str(tr),
                "-l", str(tl),
                "-o", str(out),
                "--ffmpeg-dir", str(Path(ff).parent),
            ])
            self.assertEqual(rc, 0)
            self.assertTrue(out.is_file())

            info = _probe_json(fp, out, show_chapters=True)
            self.assertIn("mov_text", [s["codec_name"] for s in info["streams"]])
            self.assertEqual(len(info["chapters"]), 2)

            out_burn = p / "final_burn.mp4"
            rc_burn = cli_main([
                "--local",
                "--burn",
                "-v", str(src),
                "-t", str(tr),
                "-l", str(tl),
                "-o", str(out_burn),
                "--ffmpeg-dir", str(Path(ff).parent),
            ])
            self.assertEqual(rc_burn, 0)
            self.assertTrue(out_burn.is_file())
            info_burn = _probe_json(fp, out_burn)
            self.assertNotIn("subtitle", [s["codec_type"] for s in info_burn["streams"]])

    def test_cli_url_flow_with_mocked_downloader(self):
        ff, fp = _get_ffmpeg_and_ffprobe()
        if not (ff and fp):
            self.skipTest("ffmpeg/ffprobe binaries not available")

        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            src = p / "downloaded.mp4"
            _make_sample_mp4(ff, src, duration=4)
            tr = p / "tr.md"
            tr.write_text("主讲人(00:00:01):\n在线下载测试。\n", encoding="utf-8")
            tl = p / "tl.md"
            tl.write_text("00:00:00 **测试章节。**说明\n", encoding="utf-8")
            out = p / "online_final.mp4"

            from link2video.downloader import MeetingMeta
            fake_dl = DownloadResult(
                title="在线测试会议",
                video_path=src,
                transcript_path=tr,
                timeline_path=tl,
                meta=MeetingMeta("https://meeting.tencent.com/crm/XYZ", "XYZ", "sh", "m", "r", "在线测试会议"),
            )
            with patch("link2video.cli.download_meeting", return_value=fake_dl):
                rc = cli_main([
                    "https://meeting.tencent.com/crm/XYZ",
                    "-o", str(out),
                    "--ffmpeg-dir", str(Path(ff).parent),
                ])
            self.assertEqual(rc, 0)
            self.assertTrue(out.is_file())

    def test_cli_help_under_cp1252_stdout(self):
        buf = io.BytesIO()
        fake_out = io.TextIOWrapper(buf, encoding="cp1252", errors="strict")
        try:
            with patch("sys.stdout", fake_out):
                with self.assertRaises(SystemExit) as ctx:
                    cli_main(["--help"])
            self.assertEqual(ctx.exception.code, 0)
            fake_out.flush()
            text = buf.getvalue().decode("utf-8")
            self.assertIn("Convert Tencent Meeting recording links", text)
        finally:
            fake_out.detach()


if __name__ == "__main__":
    unittest.main()

import tempfile
import unittest
from pathlib import Path

from voov_exporter._utils import parse_hms_to_sec
from voov_exporter.chapters import generate_ffmetadata, parse_timeline
from voov_exporter.media import find_binary, probe_video_duration
from voov_exporter.processor import process_all
from voov_exporter.subtitles import (
    chunk_text,
    fmt_srt,
    generate_srt,
    nlen,
    parse_transcript,
    split_clauses,
)

LOCAL_FFMPEG_DIR = Path(r"D:\ffmpeg-9.0-full_build")


class TestSubtitles(unittest.TestCase):
    def test_nlen(self):
        self.assertEqual(nlen("hello world"), 10)
        self.assertEqual(nlen("你好 世界\n"), 4)

    def test_parse_hms(self):
        self.assertEqual(parse_hms_to_sec("01", "02", "03"), 3723.0)
        self.assertEqual(parse_hms_to_sec(None, "02", "03"), 123.0)

    def test_fmt_srt(self):
        self.assertEqual(fmt_srt(0), "00:00:00,000")
        self.assertEqual(fmt_srt(75.5), "00:01:15,500")
        self.assertEqual(fmt_srt(3661.123), "01:01:01,123")

    def test_split_clauses(self):
        clauses = split_clauses("你好，世界！这是一段测试：好的。")
        self.assertEqual(clauses, ["你好，", "世界！", "这是一段测试：", "好的。"])

    def test_chunk_text_respects_max_cue(self):
        text = "大家好，今天我们来讨论一下系统的架构设计。首先来看第一部分。"
        cues = chunk_text(text, max_cue=20, min_cue=10)
        self.assertGreaterEqual(len(cues), 2)
        for cue in cues:
            self.assertLessEqual(nlen(cue), 20)

    def test_parse_transcript_inline_and_two_line_md_with_bom(self):
        sample = (
            "\ufeff周立杰-发言人1(00:00:05):\n"
            "大家好，今天开会。\n"
            "\n"
            "李四（01:30）: 好的，收到。\n"
            "内容由 AI 生成，仅供参考\n"
        )
        paras, skipped = parse_transcript(sample)
        self.assertEqual(len(paras), 2)
        self.assertEqual(paras[0], ("周立杰-发言人1", 5.0, "大家好，今天开会。"))
        self.assertEqual(paras[1], ("李四", 90.0, "好的，收到。"))
        self.assertEqual(skipped, ["内容由 AI 生成，仅供参考"])

    def test_generate_srt_non_overlapping(self):
        paras = [
            ("周立杰-发言人1", 5.0, "第一句非常短，第二句也很短，第三句还是短。"),
            ("周立杰-发言人2", 6.5, "接得很快的一句话。"),
        ]
        srt = generate_srt(paras, video_dur=10.0)
        self.assertIn("发言人1: 第一句", srt)
        self.assertIn("发言人2: 接得很快", srt)

        blocks = [b for b in srt.strip().split("\n\n") if b.strip()]
        prev_end = ""
        for b in blocks:
            time_line = b.splitlines()[1]
            start, end = time_line.split(" --> ")
            self.assertLessEqual(start, end)
            if prev_end:
                self.assertGreaterEqual(start, prev_end)
            prev_end = end


class TestChapters(unittest.TestCase):
    def test_parse_timeline_strips_markdown_bold_and_bom(self):
        sample = (
            "\ufeff00:01:16 **会议开场并介绍本节周常内容。**周立杰-发言人1提醒签到码。\n"
            "00:05:24 **课堂转向 GitHub 平台基础功能讲解。**详细说明。\n"
            "内容由 AI 生成，仅供参考\n"
        )
        chapters, skipped = parse_timeline(sample)
        self.assertEqual(len(chapters), 2)
        self.assertEqual(chapters[0], (76.0, "会议开场并介绍本节周常内容"))
        self.assertEqual(chapters[1], (324.0, "课堂转向 GitHub 平台基础功能讲解"))
        self.assertEqual(skipped, ["内容由 AI 生成，仅供参考"])

    def test_generate_ffmetadata_and_errors(self):
        chapters = [(0.0, "开场"), (30.0, "正文")]
        meta = generate_ffmetadata(chapters, video_dur=60.0)
        self.assertIn(";FFMETADATA1", meta)
        self.assertIn("START=0", meta)
        self.assertIn("END=30000", meta)
        self.assertIn("title=01 开场", meta)
        self.assertIn("title=02 正文", meta)

        with self.assertRaises(ValueError):
            generate_ffmetadata([], video_dur=60.0)
        with self.assertRaises(ValueError):
            generate_ffmetadata([(30.0, "B"), (10.0, "A")], video_dur=60.0)


class TestMediaAndProcessor(unittest.TestCase):
    def test_find_ffmpeg_binaries_in_custom_dir(self):
        if not LOCAL_FFMPEG_DIR.is_dir():
            self.skipTest("local ffmpeg dir not present")
        self.assertIsNotNone(find_binary("ffmpeg", ffmpeg_dir=LOCAL_FFMPEG_DIR))
        self.assertIsNotNone(find_binary("ffprobe", ffmpeg_dir=LOCAL_FFMPEG_DIR))

    def test_process_all_uses_temp_dir_and_validates_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            tr = tmp_path / "t.md"
            tl = tmp_path / "c.md"
            tr.write_text("张三(00:00:05):\n大家好。\n\n李四(00:00:35):\n收到。\n", encoding="utf-8")
            tl.write_text("00:00:00 **开场。**说明\n00:00:30 **正文。**说明\n", encoding="utf-8")

            srt_p, meta_p, stats = process_all(
                transcript_path=tr,
                timeline_path=tl,
                video_dur=60.0,
                out_dir=tmp_path / "out",
                base_name="unit_test",
            )
            self.assertTrue(srt_p.is_file())
            self.assertTrue(meta_p.is_file())
            self.assertEqual(stats.paragraphs, 2)
            self.assertEqual(stats.chapters, 2)

            with self.assertRaises(FileNotFoundError):
                process_all(transcript_path=tmp_path / "missing.txt", video_dur=60.0, out_dir=tmp_path)
            with self.assertRaises(ValueError):
                process_all(transcript_path=tr, out_dir=tmp_path)

    def test_bom_file_on_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            tl = tmp_path / "bom_timeline.txt"
            tl.write_bytes("\ufeff00:00:00 **第一章。**说明\n00:00:30 **第二章。**说明\n".encode("utf-8"))
            _, meta_p, stats = process_all(timeline_path=tl, video_dur=60.0, out_dir=tmp_path)
            content = meta_p.read_text(encoding="utf-8")
            self.assertEqual(stats.chapters, 2)
            self.assertIn("title=01 第一章", content)

    def test_real_material_end_to_end(self):
        repo_root = Path(__file__).resolve().parent.parent
        real_tr = repo_root / "material" / "20261106184953-CPU Weekly Sharing Session W3-逐字稿文本-1.md"
        real_tl = repo_root / "material" / "20261106184953-CPU Weekly Sharing Session W3-时间轴文本-1.md"
        real_mp4 = repo_root / "material" / "20261106184953-CPU Weekly Sharing Session W3-视频-1-共享屏幕.mp4"
        if not (real_tr.is_file() and real_tl.is_file() and real_mp4.is_file()):
            self.skipTest("material fixtures not present")

        # Verify native MP4 parser and ffprobe (if installed) agree
        native_dur = probe_video_duration(real_mp4)
        self.assertAlmostEqual(native_dur, 8071.68, places=2)
        if LOCAL_FFMPEG_DIR.is_dir():
            ffprobe_dur = probe_video_duration(real_mp4, ffmpeg_dir=LOCAL_FFMPEG_DIR)
            self.assertAlmostEqual(ffprobe_dur, 8071.68, places=2)

        with tempfile.TemporaryDirectory() as tmp:
            srt_p, meta_p, stats = process_all(
                transcript_path=real_tr,
                timeline_path=real_tl,
                video_path=real_mp4,
                out_dir=tmp,
                base_name="w3",
            )
            self.assertEqual(stats.paragraphs, 246)
            self.assertEqual(stats.skipped_transcript_lines, [])
            self.assertEqual(stats.chapters, 12)
            self.assertEqual(stats.skipped_timeline_lines, ["内容由 AI 生成，仅供参考"])
            self.assertGreater(stats.srt_cues, 1000)

            srt_text = srt_p.read_text(encoding="utf-8")
            meta_text = meta_p.read_text(encoding="utf-8")
            self.assertIn("发言人1:", srt_text)
            self.assertNotIn("**", meta_text)
            self.assertIn("title=01 会议开场并介绍本节周常内容", meta_text)
            self.assertIn("END=8071680", meta_text)


if __name__ == "__main__":
    unittest.main()

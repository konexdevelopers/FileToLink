"""
Offline unit tests for the audio-track / HLS feature (no Telegram, no network, no FFmpeg binary needed).

Run from the project root:

    python -m unittest tests.test_audio_tracks -v
"""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from TechVJ.util import hls_manager as hm  # noqa: E402
from TechVJ.util import media_probe as mp  # noqa: E402

# ---- real `ffmpeg -i` banners ------------------------------------------------------------------

BANNER_MKV = """\
Input #0, matroska,webm, from 'movie.mkv':
  Metadata:
    ENCODER         : Lavf61.1.100
  Duration: 00:00:12.02, start: -0.021000, bitrate: 841 kb/s
  Stream #0:0: Video: h264 (High), yuv420p(tv, bt709, progressive), 1920x1080 [SAR 1:1 DAR 16:9], 23.98 fps, 23.98 tbr, 1k tbn
    Metadata:
      DURATION        : 00:00:12.000000000
  Stream #0:1(eng): Audio: ac3, 48000 Hz, 5.1(side), fltp, 448 kb/s (default)
    Metadata:
      DURATION        : 00:00:12.000000000
  Stream #0:2(kok): Audio: aac (LC), 48000 Hz, mono, fltp
    Metadata:
      title           : Konkani 2.0
  Stream #0:3: Audio: aac (LC), 48000 Hz, mono, fltp
  Stream #0:4(eng): Audio: aac (LC), 48000 Hz, stereo, fltp
    Metadata:
      title           : Commentary
  Stream #0:5(eng): Subtitle: subrip (default)
At least one output file must be specified
"""

# the exact shapes quoted in the task description
BANNER_ISSUE_EXAMPLES = """\
Input #0, matroska,webm, from 'x.mkv':
  Duration: 01:40:00.00, start: 0.000000, bitrate: 5000 kb/s
  Stream #0:0: Video: h264 (High), yuv420p, 1280x720, 24 fps, 24 tbr, 1k tbn
  Stream #0:1(eng): Audio: aac (LC), 48000 Hz, stereo, fltp (default)
  Stream #0:2(eng): Audio: ac3, 48000 Hz, 5.1(side), fltp, 384 kb/s
  Stream #0:3(hin): Audio: aac (LC), 48000 Hz, stereo, fltp
"""

# MPEG-TS style: hex id in brackets, language after it
BANNER_TS = """\
Input #0, mpegts, from 'show.ts':
  Duration: 00:45:00.00, start: 1.400000, bitrate: 4000 kb/s
  Program 1
  Stream #0:0[0x100]: Video: h264 (Main) ([27][0][0][0] / 0x001B), yuv420p(tv, bt709, progressive), 1280x720 [SAR 1:1 DAR 16:9], 25 fps, 25 tbr, 90k tbn
  Stream #0:1[0x101](tam): Audio: ac3 ([129][0][0][0] / 0x0081), 48000 Hz, stereo, fltp, 192 kb/s
  Stream #0:2[0x102](tel): Audio: ac3 ([129][0][0][0] / 0x0081), 48000 Hz, stereo, fltp, 192 kb/s
  Stream #0:3[0x103]: Audio: mp2 ([3][0][0][0] / 0x0003), 48000 Hz, stereo, fltp, 128 kb/s
"""

BANNER_COVER_ART = """\
Input #0, matroska,webm, from 'a.mkv':
  Duration: 00:03:00.00, start: 0.000000, bitrate: 200 kb/s
  Stream #0:0: Video: mjpeg (Baseline), yuvj444p(pc), 600x600, 90k tbr, 90k tbn (attached pic)
  Stream #0:1: Video: h264 (High), yuv420p, 640x360, 25 fps, 25 tbr, 1k tbn
  Stream #0:2(jpn): Audio: opus, 48000 Hz, stereo, fltp
"""


class LanguageTests(unittest.TestCase):
    def test_codes_from_the_task(self):
        expected = {"eng": "English", "en": "English", "hin": "Hindi", "hi": "Hindi", "tam": "Tamil", "ta": "Tamil",
                    "tel": "Telugu", "te": "Telugu", "jpn": "Japanese", "kor": "Korean"}
        for code, name in expected.items():
            self.assertEqual(mp.normalize_language(code)[1], name, code)

    def test_case_and_region_subtags(self):
        self.assertEqual(mp.normalize_language("ENG")[1], "English")
        self.assertEqual(mp.normalize_language("en-US"), ("en", "English"))
        self.assertEqual(mp.normalize_language("pt_BR"), ("pt", "Portuguese"))

    def test_missing_or_undetermined_is_never_guessed(self):
        for raw in (None, "", " ", "und", "UND", "zxx", "mul", "mis", "qaa", "qtz", "??", "12", "toolongcode"):
            self.assertEqual(mp.normalize_language(raw), ("", ""), repr(raw))

    def test_real_but_unmapped_code_keeps_the_code_not_a_made_up_name(self):
        code, name = mp.normalize_language("xyz")
        self.assertEqual((code, name), ("xyz", ""))

    def test_bcp47_tag_for_the_master_playlist(self):
        self.assertEqual(mp.LANGUAGE_ALPHA2["hin"], "hi")
        self.assertEqual(mp.LANGUAGE_ALPHA2["fre"], "fr")


class BannerParserTests(unittest.TestCase):
    def test_mixed_real_banner(self):
        result = mp.parse_ffmpeg_info(BANNER_MKV)
        labels = [t["label"] for t in result.audio]
        self.assertEqual(labels, ["English", "Konkani", "Track 3", "English - Commentary"])
        self.assertEqual(result.audio[0]["code"], "eng")
        self.assertEqual(result.audio[1]["language"], "Konkani")
        self.assertEqual(result.audio[2]["label"], "Track 3")              # no metadata -> never invented
        self.assertEqual(result.audio[2]["code"], "")
        self.assertEqual(result.audio[3]["title"], "Commentary")
        self.assertEqual([t["index"] for t in result.audio], [0, 1, 2, 3])
        self.assertEqual([t["stream"] for t in result.audio], [1, 2, 3, 4])  # absolute stream ids
        self.assertTrue(result.audio[0]["default"])
        self.assertEqual(sum(1 for t in result.audio if t["default"]), 1)
        self.assertEqual(result.audio[0]["channels"], 6)
        self.assertEqual(result.video.codec, "h264")
        self.assertEqual((result.video.width, result.video.height), (1920, 1080))
        self.assertAlmostEqual(result.duration, 12.02, places=2)

    def test_subtitle_streams_are_not_audio(self):
        self.assertEqual(len(mp.parse_ffmpeg_info(BANNER_MKV).audio), 4)

    def test_examples_from_the_task_description(self):
        result = mp.parse_ffmpeg_info(BANNER_ISSUE_EXAMPLES)
        self.assertEqual([(t["code"], t["stream"]) for t in result.audio], [("eng", 1), ("eng", 2), ("hin", 3)])
        self.assertEqual(result.audio[2]["language"], "Hindi")
        # two English tracks must not end up with the same label
        self.assertEqual(len({t["label"] for t in result.audio}), 3)

    def test_mpegts_hex_ids_and_missing_language(self):
        result = mp.parse_ffmpeg_info(BANNER_TS)
        self.assertEqual([t["label"] for t in result.audio], ["Tamil", "Telugu", "Track 3"])
        self.assertEqual(result.audio[2]["code"], "")

    def test_cover_art_is_not_the_video_stream(self):
        result = mp.parse_ffmpeg_info(BANNER_COVER_ART)
        self.assertEqual(result.video.codec, "h264")
        self.assertEqual(result.video.stream_index, 1)
        self.assertEqual(result.audio[0]["language"], "Japanese")

    def test_garbage_does_not_crash(self):
        for text in ("", "nothing useful here", "Stream #0:1: Audio:", "Stream #9:9(xx): Audio: x"):
            result = mp.parse_ffmpeg_info(text)
            self.assertIsInstance(result.audio, list)


class FfprobeParserTests(unittest.TestCase):
    DATA = {
        "format": {"format_name": "matroska,webm", "duration": "7200.5", "bit_rate": "4000000"},
        "streams": [
            {"index": 0, "codec_type": "video", "codec_name": "h264", "profile": "High", "pix_fmt": "yuv420p",
             "width": 1920, "height": 1080, "level": 41, "avg_frame_rate": "24000/1001", "disposition": {}},
            {"index": 1, "codec_type": "video", "codec_name": "mjpeg", "disposition": {"attached_pic": 1}},
            {"index": 2, "codec_type": "audio", "codec_name": "eac3", "channels": 6, "channel_layout": "5.1(side)",
             "sample_rate": "48000", "tags": {"language": "eng", "title": "English 5.1"}, "disposition": {"default": 1}},
            {"index": 3, "codec_type": "audio", "codec_name": "aac", "channels": 2, "channel_layout": "stereo",
             "tags": {"LANGUAGE": "HIN"}, "disposition": {"default": 0}},
            {"index": 4, "codec_type": "audio", "codec_name": "aac", "channels": 2, "tags": {"language": "und"}, "disposition": {}},
            {"index": 5, "codec_type": "subtitle", "codec_name": "subrip", "tags": {"language": "eng"}},
        ],
    }

    def test_parse(self):
        result = mp.parse_ffprobe(self.DATA)
        self.assertEqual([t["label"] for t in result.audio], ["English", "Hindi", "Track 3"])
        self.assertEqual(result.audio[0]["title"], "English 5.1")
        self.assertEqual(result.audio[0]["codec"], "eac3")
        self.assertEqual(result.video.stream_index, 0)       # not the attached picture
        self.assertEqual(result.video.level, 41)
        self.assertAlmostEqual(result.video.fps, 23.976, places=2)
        self.assertEqual(result.duration, 7200.5)

    def test_no_default_flag_means_first_track(self):
        data = {"streams": [{"index": 1, "codec_type": "audio", "tags": {"language": "eng"}},
                            {"index": 2, "codec_type": "audio", "tags": {"language": "hin"}}]}
        result = mp.parse_ffprobe(data)
        self.assertEqual([t["default"] for t in result.audio], [True, False])

    def test_duplicate_labels_are_made_unique(self):
        data = {"streams": [{"index": 1, "codec_type": "audio", "channels": 6, "channel_layout": "5.1", "tags": {"language": "eng"}},
                            {"index": 2, "codec_type": "audio", "channels": 2, "channel_layout": "stereo", "tags": {"language": "eng"}},
                            {"index": 3, "codec_type": "audio", "channels": 2, "channel_layout": "stereo", "tags": {"language": "eng"}}]}
        labels = [t["label"] for t in mp.parse_ffprobe(data).audio]
        self.assertEqual(len(set(labels)), 3, labels)

    def test_metadata_text_is_sanitised(self):
        data = {"streams": [{"index": 1, "codec_type": "audio", "tags": {"title": "a\x00b\nc" + "x" * 500}}]}
        title = mp.parse_ffprobe(data).audio[0]["title"]
        self.assertNotIn("\x00", title)
        self.assertNotIn("\n", title)
        self.assertLessEqual(len(title), 80)


class VideoPlanTests(unittest.TestCase):
    def video(self, **kw):
        base = dict(codec="h264", profile="High", pix_fmt="yuv420p", width=1920, height=1080, level=41)
        base.update(kw)
        return mp.VideoInfo(**base)

    def test_plain_h264_is_copied(self):
        self.assertEqual(mp.decide_video_mode(self.video(), True)[0], "copy")

    def test_everything_else_is_transcoded_never_assumed_h264(self):
        for kw in (dict(codec="hevc"), dict(codec="vp9"), dict(codec="av1"), dict(codec="mpeg4"),
                   dict(pix_fmt="yuv420p10le", profile="High 10"), dict(profile="High 4:2:2", pix_fmt="yuv422p"), dict(level=62)):
            self.assertEqual(mp.decide_video_mode(self.video(**kw), True)[0], "transcode", kw)

    def test_transcode_disabled(self):
        self.assertEqual(mp.decide_video_mode(self.video(codec="hevc"), False)[0], None)
        self.assertEqual(mp.decide_video_mode(self.video(), False)[0], "copy")
        self.assertEqual(mp.decide_video_mode(None, True)[0], None)

    def test_codec_string_is_real_or_absent(self):
        self.assertEqual(mp.avc1_codec_string(self.video(profile="High", level=41), "copy", 1080), "avc1.640029")
        self.assertEqual(mp.avc1_codec_string(self.video(profile="Main", level=31), "copy", 720), "avc1.4d401f")
        self.assertEqual(mp.avc1_codec_string(self.video(profile="Constrained Baseline", level=30), "copy", 360), "avc1.42e01e")
        self.assertIsNone(mp.avc1_codec_string(self.video(level=None), "copy", 1080))     # unknown -> omitted, not guessed
        self.assertEqual(mp.avc1_codec_string(self.video(), "transcode", 1080), "avc1.640029")
        self.assertEqual(mp.avc1_codec_string(self.video(), "transcode", 2160), "avc1.640033")


class MasterPlaylistTests(unittest.TestCase):
    TRACKS = [
        {"label": "English", "code": "eng", "default": True},
        {"label": 'Hin"di\n', "code": "hin", "default": False},
        {"label": "Track 3", "code": "", "default": False},
    ]

    def test_structure(self):
        text = hm.build_master_playlist(self.TRACKS, "avc1.640028", 3_000_000, "1920x1080", 23.976)
        lines = text.splitlines()
        self.assertEqual(lines[0], "#EXTM3U")
        media = [l for l in lines if l.startswith("#EXT-X-MEDIA:")]
        self.assertEqual(len(media), 3)
        self.assertTrue(all("TYPE=AUDIO" in l and 'GROUP-ID="audio"' in l for l in media))
        self.assertEqual(sum("DEFAULT=YES" in l for l in media), 1)
        self.assertIn('LANGUAGE="en"', media[0])
        self.assertIn('LANGUAGE="hi"', media[1])
        self.assertNotIn("LANGUAGE", media[2])                      # no metadata -> no language attribute
        self.assertIn('URI="audio_2.m3u8"', media[2])
        self.assertNotIn('"\n', media[1])
        self.assertEqual(media[1].count('"') % 2, 0)                # quotes stay balanced after escaping
        stream = lines[lines.index("video.m3u8") - 1]
        self.assertIn('AUDIO="audio"', stream)
        self.assertIn('CODECS="avc1.640028,mp4a.40.2"', stream)
        self.assertIn("RESOLUTION=1920x1080", stream)

    def test_codecs_omitted_when_unknown(self):
        text = hm.build_master_playlist(self.TRACKS[:1], None, 1_000_000, None, None)
        self.assertNotIn("CODECS", text)
        self.assertNotIn("RESOLUTION", text)

    def test_bandwidth_estimate(self):
        probe = mp.ProbeResult(video=mp.VideoInfo(bit_rate=2_000_000), bit_rate=2_400_000, duration=100)
        self.assertEqual(hm.estimate_bandwidth(probe, "copy", 1920, 1080, "128k", 0), 2_128_000)
        probe = mp.ProbeResult(video=mp.VideoInfo(fps=24), duration=100)
        self.assertEqual(hm.estimate_bandwidth(probe, "copy", 1920, 1080, "128k", 125_000_000), 10_000_000 + 128_000)


class CommandTests(unittest.TestCase):
    def setUp(self):
        os.environ.pop("HLS_MAX_HEIGHT", None)
        self.cfg = hm.load_config()
        self.tracks = [{"stream": 1, "index": 0}, {"stream": 2, "index": 1}, {"stream": 5, "index": 2}]

    def command(self, mode, **video_kw):
        video = mp.VideoInfo(stream_index=0, codec="h264", width=1920, height=1080, **video_kw)
        return hm.build_ffmpeg_command("/usr/bin/ffmpeg", "http://127.0.0.1:8080/abcdef12", Path("/c/12"), self.tracks, video, mode, self.cfg)

    def test_one_output_per_audio_track_and_one_video_output(self):
        cmd = self.command("copy")
        self.assertEqual(cmd.count("-f"), 4)
        for name in ("video.m3u8", "audio_0.m3u8", "audio_1.m3u8", "audio_2.m3u8"):
            self.assertIn(str(Path("/c/12") / name), cmd)
        self.assertEqual([cmd[i + 1] for i, a in enumerate(cmd) if a == "-map"], ["0:0", "0:1", "0:2", "0:5"])

    def test_copy_does_not_reencode_video_transcode_does(self):
        copy = self.command("copy")
        self.assertIn("copy", copy[copy.index("-c:v") + 1])
        self.assertNotIn("libx264", copy)
        trans = self.command("transcode")
        self.assertIn("libx264", trans)
        self.assertIn("yuv420p", trans)

    def test_audio_is_always_stereo_aac(self):
        cmd = self.command("copy")
        self.assertEqual(cmd.count("aac"), 3)
        self.assertEqual(cmd.count("-ac"), 3)

    def test_input_is_restricted_and_no_shell_metacharacter_surface(self):
        cmd = self.command("copy")
        self.assertEqual(cmd[cmd.index("-protocol_whitelist") + 1], "http,tcp")
        self.assertEqual(cmd[cmd.index("-i") + 1], "http://127.0.0.1:8080/abcdef12")
        self.assertEqual(cmd.count("-i"), 1)
        self.assertTrue(all(isinstance(part, str) for part in cmd))

    def test_segments_are_written_atomically(self):
        cmd = self.command("copy")
        self.assertTrue(any("temp_file" in part for part in cmd))
        self.assertIn("event", cmd)

    def test_scale_when_max_height_is_set(self):
        os.environ["HLS_MAX_HEIGHT"] = "720"
        try:
            cfg = hm.load_config()
        finally:
            os.environ.pop("HLS_MAX_HEIGHT", None)
        video = mp.VideoInfo(stream_index=0, codec="hevc", width=3840, height=2160)
        cmd = hm.build_ffmpeg_command("/f", "http://x", Path("/c/1"), self.tracks[:1], video, "transcode", cfg)
        self.assertIn("scale=1280:720", cmd)


class SpsTests(unittest.TestCase):
    @staticmethod
    def ts_packet(pid, payload, start=False):
        header = bytes([0x47, (0x40 if start else 0) | (pid >> 8), pid & 0xFF, 0x10])
        return header + payload.ljust(184, b"\xff")

    def test_codec_from_sps_even_when_split_across_packets(self):
        sps = b"\x00\x00\x01\x67\x64\x00\x28\xac"             # High, constraints 0, level 4.0
        stream = b"\x00\x00\x01\xe0\x00\x00\x80\x00\x00" + b"\xaa" * 173 + sps[:2]   # exactly 184 payload bytes: SPS is cut in half
        data = self.ts_packet(0x100, stream, True) + self.ts_packet(0x100, sps[2:] + b"\x11" * 20)
        path = Path(os.environ.get("TMPDIR", "/tmp")) / "sps_test.ts"
        path.write_bytes(data)
        try:
            self.assertEqual(hm.h264_codec_from_ts(path), "avc1.640028")
        finally:
            path.unlink()

    def test_missing_sps_returns_none(self):
        path = Path(os.environ.get("TMPDIR", "/tmp")) / "nosps_test.ts"
        path.write_bytes(self.ts_packet(0x100, b"\x00" * 100, True))
        try:
            self.assertIsNone(hm.h264_codec_from_ts(path))
        finally:
            path.unlink()
        self.assertIsNone(hm.h264_codec_from_ts(Path("/definitely/not/here.ts")))


class PlaylistStatsTests(unittest.TestCase):
    def test_counts(self):
        self.assertEqual(hm.playlist_stats("#EXTINF:4,\na.ts\n#EXTINF:4,\nb.ts\n"), {"segments": 2, "ended": 0})
        self.assertEqual(hm.playlist_stats("#EXTINF:4,\na.ts\n#EXT-X-ENDLIST\n"), {"segments": 1, "ended": 1})
        self.assertEqual(hm.playlist_stats(""), {"segments": 0, "ended": 0})


if __name__ == "__main__":
    unittest.main(verbosity=2)

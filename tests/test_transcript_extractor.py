import unittest
from types import SimpleNamespace
from unittest.mock import patch

from youtube_transcript_api._errors import CouldNotRetrieveTranscript

from transcript_extractor import TranscriptExtractor


class _Track:
    def __init__(self, language_code, generated, text):
        self.language_code = language_code
        self.is_generated = generated
        self._text = text

    def fetch(self):
        return [SimpleNamespace(text=self._text)]


class _TranscriptList:
    def __init__(self, tracks):
        self._tracks = tracks

    def __iter__(self):
        return iter(self._tracks)


class _FailingTrack(_Track):
    def fetch(self):
        raise CouldNotRetrieveTranscript("temporary caption fetch failure")


def _extractor(tracks):
    extractor = TranscriptExtractor.__new__(TranscriptExtractor)
    extractor.preferred_languages = ["ko", "en"]
    extractor.fallback_to_any = True
    extractor.min_transcript_chars = 200
    extractor.api = SimpleNamespace(list=lambda _video_id: _TranscriptList(tracks))
    extractor.last_track_info = None
    extractor.engine = "youtube-transcript-api"
    extractor.model = None
    return extractor


class TranscriptExtractorTest(unittest.TestCase):
    def test_caption_segments_are_original_timed_and_cleared_on_next_video(self):
        track = _Track("ko", True, "unused")
        track.fetch = lambda: [SimpleNamespace(text="원문 자막 " * 50, start=12.5, duration=3.0)]
        extractor = _extractor([track])
        self.assertEqual(extractor.extract_with_status("first")[1], "ok")
        self.assertEqual(extractor.last_segments[0]["start"], 12.5)
        self.assertEqual(extractor.last_segments[0]["text"], "원문 자막 " * 50)
        extractor.api = SimpleNamespace(list=lambda _: _TranscriptList([]))
        self.assertEqual(extractor.extract_with_status("next")[1], "no_captions")
        self.assertIsNone(extractor.last_segments)

    def test_single_proxy_env_is_used_for_http_and_https(self):
        extractor = TranscriptExtractor.__new__(TranscriptExtractor)
        with patch.dict(
            "os.environ",
            {"YOUTUBE_TRANSCRIPT_PROXY_URL": "http://user:pass@proxy:8080"},
            clear=False,
        ):
            proxy = extractor._build_proxy_config({})

        self.assertEqual(
            proxy.to_requests_dict(),
            {
                "http": "http://user:pass@proxy:8080",
                "https": "http://user:pass@proxy:8080",
            },
        )

    def test_proxy_env_overrides_yaml_without_logging_the_secret(self):
        extractor = TranscriptExtractor.__new__(TranscriptExtractor)
        with patch.dict(
            "os.environ",
            {"YOUTUBE_TRANSCRIPT_PROXY_URL": "https://env-user:secret@proxy:443"},
            clear=False,
        ):
            proxy = extractor._build_proxy_config(
                {
                    "proxy": {
                        "http_url": "http://yaml-user:secret@old:80",
                        "https_url": "http://yaml-user:secret@old:80",
                    }
                }
            )

        self.assertEqual(
            proxy.to_requests_dict()["https"],
            "https://env-user:secret@proxy:443",
        )

    def test_rejects_non_http_proxy_scheme(self):
        extractor = TranscriptExtractor.__new__(TranscriptExtractor)
        with patch.dict(
            "os.environ",
            {"YOUTUBE_TRANSCRIPT_PROXY_URL": "socks5://proxy:1080"},
            clear=False,
        ), self.assertRaisesRegex(ValueError, "http:// or https://"):
            extractor._build_proxy_config({})

    def test_short_manual_caption_falls_back_to_generated_track(self):
        extractor = _extractor([
            _Track("ko", False, "."),
            _Track("ko", True, "자동 생성 자막 " * 100),
        ])

        text, status = extractor.extract_with_status("video")

        self.assertEqual(status, "ok")
        self.assertGreaterEqual(len(text), 200)
        self.assertEqual(
            extractor.last_track_info,
            {"language": "ko", "generated": True},
        )

    def test_all_too_short_caption_tracks_are_not_accepted(self):
        extractor = _extractor([
            _Track("ko", False, "."),
            _Track("ko", True, "짧음"),
        ])

        text, status = extractor.extract_with_status("video")

        self.assertIsNone(text)
        self.assertEqual(status, "no_captions")

    def test_partial_track_fetch_failure_is_not_classified_as_no_captions(self):
        extractor = _extractor([
            _Track("ko", False, "."),
            _FailingTrack("ko", True, ""),
        ])

        text, status = extractor.extract_with_status("video")

        self.assertIsNone(text)
        self.assertEqual(status, "error")


if __name__ == "__main__":
    unittest.main()

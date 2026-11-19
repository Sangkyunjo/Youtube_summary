import json
import queue_consumer
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from realtime_fallback import CAPTION_MODEL, PlaybackQueue
from test_state_manager import state_at
from unittest.mock import patch


class _Transcriber:
    def __init__(self, result, *, engine):
        self.result = result
        self.engine = engine
        self.model = None
        self.last_track_info = None

    def extract_with_status(self, video_id):
        return self.result(video_id) if callable(self.result) else self.result


class QueueConsumerTest(unittest.TestCase):
    def test_captions_first_does_not_construct_whisper_when_caption_is_usable(self):
        captions = _Transcriber(("충분한 자막", "ok"), engine="youtube-transcript-api")
        with (
            patch.object(queue_consumer, "_make_caption_transcriber", return_value=captions),
            patch.object(
                queue_consumer,
                "_make_whisper_transcriber",
                side_effect=AssertionError("Whisper must stay lazy"),
            ),
        ):
            extractor = queue_consumer._make_transcriber("captions-first")
            text, status = extractor.extract_with_status("video")

        self.assertEqual((text, status), ("충분한 자막", "ok"))
        self.assertEqual(extractor.engine, "youtube-transcript-api")

    def test_captions_first_uses_direct_media_for_no_captions_or_caption_block(self):
        for caption_status in ("no_captions", "blocked"):
            with self.subTest(status=caption_status):
                captions = _Transcriber(
                    (None, caption_status), engine="youtube-transcript-api"
                )
                whisper = _Transcriber(("whisper text", "ok"), engine="whisper")
                with (
                    patch.object(
                        queue_consumer,
                        "_make_caption_transcriber",
                        return_value=captions,
                    ),
                    patch.object(
                        queue_consumer,
                        "_make_whisper_transcriber",
                        return_value=whisper,
                    ),
                ):
                    extractor = queue_consumer._make_transcriber("captions-first")
                    text, status = extractor.extract_with_status("video")

                self.assertEqual((text, status), ("whisper text", "ok"))
                self.assertEqual(extractor.engine, "whisper")
                self.assertEqual(
                    extractor.fallback_reason, f"captions_{caption_status}"
                )

    def test_captions_first_does_not_use_whisper_for_transient_or_unavailable(self):
        for caption_status in ("error", "unavailable"):
            with self.subTest(status=caption_status):
                captions = _Transcriber(
                    (None, caption_status), engine="youtube-transcript-api"
                )
                with (
                    patch.object(
                        queue_consumer, "_make_caption_transcriber", return_value=captions
                    ),
                    patch.object(
                        queue_consumer,
                        "_make_whisper_transcriber",
                        side_effect=AssertionError("Whisper must not run"),
                    ),
                ):
                    extractor = queue_consumer._make_transcriber("captions-first")
                    text, status = extractor.extract_with_status("video")

                self.assertEqual((text, status), (None, caption_status))
                self.assertEqual(extractor.engine, "youtube-transcript-api")


class TooOldTest(unittest.TestCase):
    def test_utc_z_suffix_is_parsed(self):
        recent = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with patch.object(queue_consumer, "_MAX_AGE_DAYS", 30):
            self.assertTrue(queue_consumer._too_old("2020-01-01T00:00:00Z"))
            self.assertFalse(queue_consumer._too_old(recent))


class _Summarizer:
    def summarize(self, **_kwargs):
        return {"overview": "요약"}


class QueueLimitTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.queue_dir = root / "queue"
        self.inbox_dir = root / "inbox"
        self.queue_dir.mkdir()
        self.inbox_dir.mkdir()
        self.playback = PlaybackQueue(root / "realtime.sqlite3")
        self.state = state_at(root)
        published_at = datetime.now(timezone.utc).isoformat()
        self.rows = [
            {"video_id": f"video{i:06d}", "published_at": published_at}
            for i in range(5)
        ]
        self._write_queue()

    def _write_queue(self):
        (self.queue_dir / "pending_1.jsonl").write_text(
            "\n".join(json.dumps(row) for row in self.rows), encoding="utf-8"
        )

    def _wait_for_playback(self, rows):
        for row in rows:
            self.playback.enqueue(row, "captions_blocked", "narrative")

    def _run(self, result, *, limit=None, request_delay=0, only=None):
        captions = _Transcriber(result, engine="youtube-transcript-api")
        with (
            patch.object(queue_consumer, "_queue_dir", return_value=self.queue_dir),
            patch.object(queue_consumer, "_inbox_dir", return_value=self.inbox_dir),
            patch.object(queue_consumer, "StateManager", return_value=self.state),
            patch.object(queue_consumer, "PlaybackQueue", return_value=self.playback),
            patch.object(queue_consumer, "realtime_enabled", return_value=True),
            patch.object(queue_consumer, "_TRANSCRIBER", "captions-first"),
            patch.object(queue_consumer, "_MAX_AGE_DAYS", 30),
            patch.object(queue_consumer, "_REQUEST_DELAY", request_delay),
            patch.object(queue_consumer, "_make_caption_transcriber", return_value=captions),
            patch.object(queue_consumer, "Summarizer", return_value=_Summarizer()),
            patch.object(queue_consumer.time, "sleep") as sleep,
        ):
            return queue_consumer.run(limit=limit, only=only), sleep

    def _inbox_ids(self):
        return sorted(path.stem for path in self.inbox_dir.glob("*.json"))

    def _doc(self, video_id):
        return json.loads((self.inbox_dir / f"{video_id}.json").read_text(encoding="utf-8"))

    def test_only_selected_videos_are_processed_and_queue_files_stay(self):
        selected = self.rows[1]["video_id"]

        result, _sleep = self._run(("충분한 자막", "ok"), only={selected})

        self.assertEqual(result["written"], 1)
        self.assertEqual(self._inbox_ids(), [selected])
        self.assertTrue((self.queue_dir / "pending_1.jsonl").exists())

    def test_daily_completion_without_playback_allows_narrative_output(self):
        vid = self.rows[0]['video_id']
        self.state.mark_video_processed(vid, 'title', 'channel', 'prior.txt')

        result, _ = self._run(('충분한 자막', 'ok'), only={vid})

        self.assertEqual(result['written'], 1)
        self.assertIsNone(self.playback.get(vid))
        self.assertTrue(self.state.is_video_processed(vid, 'daily'))
        self.assertTrue(self.state.is_video_processed(vid, 'narrative'))

    def test_busy_narrative_claim_keeps_the_queue_for_retry(self):
        vid = self.rows[0]['video_id']
        with self.state.processing_claim(vid, 'narrative'):
            result, _ = self._run(('충분한 자막', 'ok'))
        self.assertEqual(result['busy'], 1)
        self.assertNotIn(vid, self._inbox_ids())
        self.assertTrue((self.queue_dir / 'pending_1.jsonl').exists())

        result, _ = self._run(('충분한 자막', 'ok'))
        self.assertEqual(result['written'], 1)
        self.assertIn(vid, self._inbox_ids())

    def test_archived_contract_repairs_missing_completion_without_resummarizing(self):
        row = self.rows[0]
        vid = row['video_id']
        self._wait_for_playback([row])
        archive = self.inbox_dir / '_archive'
        archive.mkdir()
        (archive / f'{vid}.json').write_text(json.dumps(dict(row, summary='Existing summary')))

        result, _ = self._run(('충분한 자막', 'ok'), only={vid})

        self.assertEqual(result['written'], 0)
        self.assertTrue(self.state.is_video_processed(vid, 'narrative'))
        self.assertFalse(self.playback.needs_delivery(vid, 'narrative'))
        self.assertEqual(self._inbox_ids(), [])

    def test_candidates_are_newest_unprocessed_videos_with_length(self):
        now = datetime.now(timezone.utc)
        for i, row in enumerate(self.rows):
            row["published_at"] = (now - timedelta(hours=10 - i)).isoformat()
        self.rows[0]["published_at"] = "2020-01-01T00:00:00Z"
        self._write_queue()
        self.state.mark_video_processed(self.rows[4]['video_id'], 'title', 'channel', 'prior.json')
        lengths = {self.rows[3]["video_id"]: {"duration_seconds": 600, "live": False},
                   self.rows[2]["video_id"]: {"duration_seconds": 0, "live": True}}

        class FakeApi:
            def get_video_durations(self, ids):
                return {vid: lengths[vid] for vid in ids if vid in lengths}

        with (
            patch.object(queue_consumer, "_queue_dir", return_value=self.queue_dir),
            patch.object(queue_consumer, "StateManager", return_value=self.state),
            patch.object(queue_consumer, "PlaybackQueue", return_value=self.playback),
            patch.object(queue_consumer, "_MAX_AGE_DAYS", 30),
            patch("youtube_api.YouTubeAPI", FakeApi),
        ):
            offer = queue_consumer.candidates(limit=5, subscriptions=[])

        self.assertEqual(offer["in_window"], 3)
        self.assertEqual([item["video_id"] for item in offer["items"]], [self.rows[3]["video_id"]])
        self.assertEqual(offer["items"][0]["duration_seconds"], 600)

    def test_candidates_include_waiting_daily_jobs_under_the_same_rules(self):
        now = datetime.now(timezone.utc).isoformat()
        self.rows = []
        self._write_queue()
        daily = {"video_id": "daily000001", "published_at": now, "channel_id": "UC1"}
        keyword = {"video_id": "kw000000001", "published_at": now, "discovery": {"mode": "keyword"}}
        asked = {"video_id": "asked000001", "published_at": now}
        for payload in (daily, keyword, asked):
            self.playback.enqueue(payload, "captions_blocked", "daily")
        self.playback.request_playback("asked000001", {"duration_seconds": 60})

        class FakeApi:
            def get_video_durations(self, ids):
                return {vid: {"duration_seconds": 120, "live": False} for vid in ids}

        with (
            patch.object(queue_consumer, "_queue_dir", return_value=self.queue_dir),
            patch.object(queue_consumer, "StateManager", return_value=self.state),
            patch.object(queue_consumer, "PlaybackQueue", return_value=self.playback),
            patch.object(queue_consumer, "_MAX_AGE_DAYS", 30),
            patch.object(queue_consumer, "_excluded_modes", return_value={"keyword"}),
            patch("youtube_api.YouTubeAPI", FakeApi),
        ):
            offer = queue_consumer.candidates(limit=5, subscriptions=[])

        self.assertEqual([entry["video_id"] for entry in offer["items"]], ["daily000001"])
        self.assertEqual(offer["items"][0]["source"], "waiting")

    def test_subscription_uploads_lead_the_offer_and_a_lookup_failure_keeps_the_backlog(self):
        now = datetime.now(timezone.utc)
        for i, row in enumerate(self.rows):
            row["published_at"] = (now - timedelta(hours=10 - i)).isoformat()
        self._write_queue()
        sub = {"video_id": "subscrib001", "title": "a &amp; b", "channel_title": "소수몽키", "channel_id": "UCs",
               "channel_handle": "@sosumonkey", "published_at": (now - timedelta(hours=20)).isoformat(),
               "url": "https://www.youtube.com/watch?v=subscrib001", "source": "subscription"}

        class FakeApi:
            def get_video_durations(self, ids):
                return {vid: {"duration_seconds": 300, "live": False} for vid in ids}

        def broken(state, days):
            raise RuntimeError("quota")

        with (
            patch.object(queue_consumer, "_queue_dir", return_value=self.queue_dir),
            patch.object(queue_consumer, "StateManager", return_value=self.state),
            patch.object(queue_consumer, "PlaybackQueue", return_value=self.playback),
            patch.object(queue_consumer, "_MAX_AGE_DAYS", 30),
            patch("youtube_api.YouTubeAPI", FakeApi),
        ):
            offer = queue_consumer.candidates(limit=3, subscriptions=[sub])
            with patch.object(queue_consumer, "_subscription_rows", broken):
                fallback = queue_consumer.candidates(limit=3)

        self.assertEqual(offer["items"][0]["video_id"], "subscrib001")
        self.assertEqual(offer["items"][0]["source"], "subscription")
        self.assertEqual(offer["items"][0]["channel_handle"], "@sosumonkey")
        self.assertEqual(offer["items"][0]["title"], "a & b")
        self.assertEqual(offer["subscriptions"], 1)
        self.assertEqual([i["source"] for i in offer["items"][1:]], ["queue", "queue"])
        self.assertIn("quota", fallback["subscription_error"])
        self.assertEqual(len(fallback["items"]), 3)

    def test_newest_videos_are_processed_first(self):
        now = datetime.now(timezone.utc)
        for i, row in enumerate(self.rows):
            row["published_at"] = (now - timedelta(hours=10 - i)).isoformat()
        self._write_queue()

        result, _sleep = self._run(("충분한 자막", "ok"), limit=2)

        self.assertEqual(result["written"], 2)
        self.assertEqual(
            self._inbox_ids(), sorted(self.rows[i]["video_id"] for i in (3, 4))
        )

    def test_recovered_transcript_is_delivered_first_without_using_the_limit(self):
        now = datetime.now(timezone.utc)
        for i, row in enumerate(self.rows):
            row["published_at"] = (now - timedelta(hours=10 - i)).isoformat()
        self.rows[1]["channel_id"] = "UCpriority"
        self._write_queue()
        oldest = self.rows[0]["video_id"]
        self._wait_for_playback(self.rows[:1])
        self.playback.claim(oldest)
        self.playback.finish(oldest, "재생으로 확보한 대본 " * 30, "local-model")
        self.state.mark_video_processed(oldest, 'title', 'channel', 'prior.txt')
        fetched = []

        def captions(vid):
            self.assertFalse(self.playback.needs_delivery(oldest, "narrative"))
            fetched.append(vid)
            return ("충분한 자막", "ok")

        config = {"youtube": {"priority_channel_ids": ["UCpriority"]}}
        with patch.object(queue_consumer, "load_config", return_value=config):
            result, _sleep = self._run(captions, limit=2)

        self.assertEqual(result["written"], 3)
        self.assertEqual(result["realtime_used"], 1)
        self.assertNotIn(oldest, fetched)
        self.assertEqual(fetched, [self.rows[1]["video_id"], self.rows[4]["video_id"]])
        self.assertFalse(self.playback.needs_delivery(oldest, "narrative"))

    def test_waiting_jobs_without_captions_do_not_use_up_the_limit(self):
        self._wait_for_playback(self.rows[:2])
        waiting = {row["video_id"] for row in self.rows[:2]}

        result, _sleep = self._run(
            lambda vid: (None, "no_captions") if vid in waiting else ("충분한 자막", "ok"),
            limit=2,
        )

        self.assertEqual(result["written"], 2)
        self.assertEqual(result["deferred"], 2)
        self.assertEqual(
            self._inbox_ids(), [row["video_id"] for row in self.rows[2:4]]
        )

    def test_recording_jobs_are_left_alone_without_a_request(self):
        self._wait_for_playback(self.rows[:3])
        for row in self.rows[:3]:
            self.playback.claim(row["video_id"])

        result, sleep = self._run(("충분한 자막", "ok"), limit=1, request_delay=5)

        self.assertEqual(result["deferred"], 3)
        self.assertEqual(result["written"], 1)
        sleep.assert_called_once_with(5)

    def test_waiting_job_recovers_when_captions_return(self):
        self._wait_for_playback(self.rows[:1])
        vid = self.rows[0]["video_id"]

        result, _sleep = self._run(("충분한 자막", "ok"))

        job = self.playback.get(vid)
        self.assertEqual((job["status"], job["model"]), ("ready", CAPTION_MODEL))
        self.assertFalse(self.playback.needs_delivery(vid, "narrative"))
        self.assertEqual(result["written"], 5)
        self.assertEqual(result["realtime_used"], 0)
        self.assertEqual(self._doc(vid)["stt"]["acquisition"], "captions")

    def test_caption_block_leaves_a_waiting_job_pending(self):
        self._wait_for_playback(self.rows[:1])

        result, _sleep = self._run((None, "blocked"))

        self.assertTrue(result["blocked"])
        self.assertEqual(self.playback.get(self.rows[0]["video_id"])["status"], "pending")

    def test_unavailable_waiting_job_is_forgotten(self):
        self._wait_for_playback(self.rows[:1])
        vid = self.rows[0]["video_id"]

        self._run((None, "unavailable"))

        self.assertIsNone(self.playback.get(vid))
        self.assertTrue(self.state.is_video_processed(vid, 'narrative'))

    def test_unavailable_narrative_output_keeps_daily_delivery(self):
        row = self.rows[0]
        vid = row['video_id']
        self._wait_for_playback([row])
        self.playback.enqueue(row, 'captions_blocked', 'daily')

        self._run((None, 'unavailable'), only={vid})

        self.assertTrue(self.state.is_video_processed(vid, 'narrative'))
        self.assertFalse(self.state.is_video_processed(vid, 'daily'))
        self.assertFalse(self.playback.needs_delivery(vid, 'narrative'))
        self.assertTrue(self.playback.needs_delivery(vid, 'daily'))

    def test_captions_kept_for_another_destination_are_labelled_captions(self):
        vid = self.rows[0]["video_id"]
        self.playback.enqueue(self.rows[0], "captions_blocked", "daily")
        self.playback.recover_with_captions(vid, "자막 " * 100, "daily", "summary.txt")

        result, _sleep = self._run(("충분한 자막", "ok"))

        self.assertEqual(result["realtime_used"], 0)
        self.assertEqual(self._doc(vid)["stt"]["acquisition"], "captions")
        self.assertFalse(self.playback.needs_delivery(vid, "narrative"))

    def test_caption_block_stops_the_run_and_leaves_the_rest_queued(self):
        result, _sleep = self._run((None, "blocked"))

        self.assertTrue(result["blocked"])
        self.assertEqual(result["written"], 0)
        self.assertEqual(result["deferred"], 1)
        self.assertIsNotNone(self.playback.get(self.rows[0]["video_id"]))
        self.assertIsNone(self.playback.get(self.rows[1]["video_id"]))

    def test_expired_waiting_jobs_are_dropped_without_fallback(self):
        expired = "2020-01-01T00:00:00Z"
        self.rows[0]["published_at"] = expired
        self.rows[1]["published_at"] = expired
        self._write_queue()
        self._wait_for_playback(self.rows[:3])
        self.playback.enqueue(
            {"video_id": "videodaily1", "published_at": expired},
            "captions_blocked",
            "daily",
        )
        self.playback.claim(self.rows[1]["video_id"])
        self.playback.finish(self.rows[1]["video_id"], "로컬 자막 " * 50, "local")

        result, _sleep = self._run(("충분한 자막", "ok"))

        self.assertIsNone(self.playback.get(self.rows[0]["video_id"]))
        self.assertIsNone(self.playback.get("videodaily1"))
        # Expired rows leave the queue before the loop; the ready transcript still delivers.
        self.assertNotIn(self.rows[0]['video_id'],
                         [row['video_id'] for row in queue_consumer._read_queue(self.queue_dir)])
        self.assertEqual(result["pruned"], {"too_old": 2})
        self.assertEqual(result["deferred"], 0)
        self.assertEqual(
            self._inbox_ids(),
            sorted(self.rows[i]["video_id"] for i in (1, 2, 3, 4)),
        )


class PruneQueueTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.queue_dir = root / "queue"
        self.queue_dir.mkdir()
        self.playback = PlaybackQueue(root / "realtime.sqlite3")
        now = datetime.now(timezone.utc)
        self.fresh = now.isoformat()
        self.old = (now - timedelta(days=45)).isoformat()

    def _write(self, name, rows):
        (self.queue_dir / name).write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")

    def _prune(self):
        with (
            patch.object(queue_consumer, "_MAX_AGE_DAYS", 30),
            patch.object(queue_consumer, "_excluded_modes", return_value={"keyword"}),
        ):
            return queue_consumer.prune_queue(self.queue_dir, self.playback)

    def test_keyword_and_expired_rows_move_to_the_archive(self):
        channel = {"mode": "channel", "query": None}
        keyword = {"mode": "keyword", "query": "HBM"}
        self._write("pending_1.jsonl", [
            {"video_id": "keep0000001", "published_at": self.fresh, "discovery": channel},
            {"video_id": "kw000000001", "published_at": self.fresh, "discovery": keyword},
            {"video_id": "old00000001", "published_at": self.old, "discovery": channel},
        ])
        self._write("pending_2.jsonl", [
            {"video_id": "old00000002", "published_at": self.old, "discovery": keyword},
        ])

        result = self._prune()

        self.assertEqual(result["pruned"], {"discovery_keyword": 2, "too_old": 1})
        self.assertFalse((self.queue_dir / "pending_2.jsonl").exists())
        kept = [row["video_id"] for row in queue_consumer._read_queue(self.queue_dir)]
        self.assertEqual(kept, ["keep0000001"])
        archived = [json.loads(line) for line in next((self.queue_dir / "_archive").glob("pruned_*.jsonl"))
                    .read_text(encoding="utf-8").splitlines()]
        self.assertEqual(sorted(row["video_id"] for row in archived),
                         ["kw000000001", "old00000001", "old00000002"])
        self.assertTrue(all(row["pruned_reason"] and row["pruned_from"] for row in archived))

    def test_waiting_jobs_of_keyword_videos_are_dropped(self):
        keyword = {"mode": "keyword", "query": "HBM"}
        self._write("pending_1.jsonl", [
            {"video_id": "kw000000001", "published_at": self.fresh, "discovery": keyword},
            {"video_id": "both0000001", "published_at": self.fresh, "discovery": keyword},
            {"video_id": "both0000001", "published_at": self.fresh, "discovery": {"mode": "channel"}},
        ])
        # The Telegram bot used to store selections without the discovery field.
        self.playback.enqueue({"video_id": "kw000000001", "published_at": self.fresh},
                              "user_selected", "narrative")
        self.playback.enqueue({"video_id": "both0000001", "published_at": self.fresh},
                              "captions_blocked", "narrative")
        self.playback.enqueue({"video_id": "daily000001", "published_at": self.fresh},
                              "captions_blocked", "daily")

        result = self._prune()

        self.assertEqual(result["dropped_jobs"], 1)
        self.assertIsNone(self.playback.get("kw000000001"))
        self.assertIsNotNone(self.playback.get("both0000001"))
        self.assertIsNotNone(self.playback.get("daily000001"))

    def test_nothing_to_prune_leaves_files_untouched(self):
        self._write("pending_1.jsonl", [{"video_id": "keep0000001", "published_at": self.fresh}])
        before = (self.queue_dir / "pending_1.jsonl").stat().st_mtime_ns

        result = self._prune()

        self.assertEqual(result, {"pruned": {}, "files": 0, "dropped_jobs": 0})
        self.assertEqual((self.queue_dir / "pending_1.jsonl").stat().st_mtime_ns, before)
        self.assertFalse((self.queue_dir / "_archive").exists())


if __name__ == "__main__":
    unittest.main()

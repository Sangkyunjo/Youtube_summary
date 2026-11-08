import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from realtime_fallback import PlaybackQueue, playback_segments, validate_playback

VID = 'abcdefghijk'


def sample(at, position, ended=False):
    return dict(observed_at=at, current_time=position, duration=60,
                video_id=VID, ad=False, muted=False, rate=1,
                paused=ended, ended=ended, buffering=False)


def ad(at):
    # During an ad the player element reports the ad's own clock and length.
    return dict(sample(at, 5), duration=15, ad=True)


class PlaybackTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.q = PlaybackQueue(Path(self.temp.name) / 'queue.sqlite3')
        self.payload = {'video_id': VID, 'title': 'Test'}

    def test_duplicate_discovery_preserves_recording_and_registers_destinations(self):
        self.q.enqueue(self.payload, 'blocked')
        self.q.claim(VID)
        self.q.enqueue(self.payload, 'no_captions', 'narrative')
        self.assertEqual(self.q.get(VID)['status'], 'recording')
        with self.assertRaises(ValueError):
            self.q.claim(VID)
        self.q.finish(VID, 'verified transcript ' * 30, 'local-model')
        self.assertEqual(len(self.q.ready('daily')), 1)
        self.assertEqual(len(self.q.ready('narrative')), 1)
        self.q.delivered(VID, 'daily', 'summary.txt')
        self.assertEqual(self.q.ready('daily'), [])
        self.assertEqual(len(self.q.ready('narrative')), 1)

    def test_failed_capture_can_retry_but_short_transcript_is_not_ready(self):
        self.q.enqueue(self.payload, 'blocked')
        self.q.claim(VID)
        self.q.fail(VID, 'silence')
        self.q.claim(VID)
        with self.assertRaises(ValueError):
            self.q.finish(VID, '.', 'model')
        self.assertEqual(self.q.ready('daily'), [])

    def test_observation_timestamp_cannot_be_supplied_after_the_fact(self):
        self.q.enqueue(self.payload, 'blocked')
        with self.assertRaises(ValueError):
            self.q.observe(VID, sample(1, 0))
        self.q.claim(VID)
        with patch('realtime_fallback.time.time', return_value=123):
            self.q.observe(VID, sample(1, 0))
        self.assertEqual(self.q.get(VID)['evidence'][0]['observed_at'], 123)

    def test_video_id_cannot_escape_data_directory(self):
        with self.assertRaises(ValueError):
            self.q.enqueue({'video_id': '../bad'}, 'blocked')

    def test_full_playback_and_tail_trim(self):
        job = dict(video_id=VID, started=100,
                   evidence=[sample(102, 0), sample(132, 30), sample(162, 60, True)])
        self.assertEqual(validate_playback(job, 67, 167), (2, 60))

    def test_ended_player_clock_stops_while_observer_arrives_later(self):
        job = dict(video_id=VID, started=100,
                   evidence=[sample(102, 0), sample(132, 30), sample(170, 60, True)])
        with self.assertRaises(ValueError):
            validate_playback(job, 80, 175)  # gap still exceeds 35 seconds
        job['evidence'].insert(2, sample(155, 53))
        self.assertEqual(validate_playback(job, 80, 175), (2, 60))

    def test_reject_ads_seek_mute_gaps_short_audio_and_wrong_video(self):
        base = dict(video_id=VID, started=100,
                    evidence=[sample(102, 0), sample(132, 30), sample(162, 60, True)])
        mutations = [
            ('ad', True), ('muted', True), ('video_id', 'other_id123'),
            ('rate', 2), ('buffering', True), ('paused', True),
            ('current_time', 45), ('observed_at', 150),
        ]
        for key, value in mutations:
            with self.subTest(key=key):
                job = copy.deepcopy(base)
                job['evidence'][1][key] = value
                with self.assertRaises(ValueError):
                    validate_playback(job, 67, 167)
        with self.assertRaises(ValueError):
            validate_playback(base, 30, 167)
        with self.assertRaises(ValueError):
            validate_playback(base, 67, 190)
        job = copy.deepcopy(base); job['evidence'][-1]['ended'] = False
        with self.assertRaises(ValueError):
            validate_playback(job, 67, 167)


class PlaybackSegmentsTest(unittest.TestCase):
    def job(self, evidence):
        return dict(video_id=VID, started=100, evidence=evidence)

    def test_uninterrupted_playback_is_one_segment(self):
        job = self.job([sample(102, 0), sample(132, 30), sample(162, 60, True)])
        self.assertEqual(playback_segments(job, 67, 167), ([(2, 62)], 60))

    def test_mid_roll_ad_is_cut_out(self):
        job = self.job([sample(102, 0), sample(122, 20), ad(122.3), ad(140),
                        sample(153, 20.2), sample(183, 50.2), sample(193, 60, True)])
        segments, duration = playback_segments(job, 95, 195)
        self.assertEqual(duration, 60)
        self.assertEqual(segments[0], (2, 22))
        self.assertAlmostEqual(segments[1][0], 53)
        self.assertAlmostEqual(segments[1][1], 92.8)

    def test_pre_roll_ad_and_buffering_are_cut_out(self):
        stall = dict(sample(131, 20), buffering=True)
        job = self.job([ad(101), sample(110, 0), sample(130, 20), stall,
                        sample(136, 20.1), sample(166, 50.1), sample(175.9, 60, True)])
        segments, _ = playback_segments(job, 80, 178)
        self.assertEqual(segments[0], (10, 30))
        self.assertAlmostEqual(segments[1][0], 36)
        self.assertAlmostEqual(segments[1][1], 75.9)

    def test_reject_seek_across_ad_long_break_gap_and_unfinished_end(self):
        cases = {
            'seek': [sample(102, 0), sample(122, 20), ad(125), sample(140, 40), sample(162, 60, True)],
            'long break': [sample(102, 0), sample(122, 20)] + [ad(122 + 30 * i) for i in range(1, 32)]
                          + [sample(1053, 20.2), sample(1093, 60, True)],
            'gap': [sample(102, 0), sample(122, 20), ad(125), ad(165), sample(170, 20.2), sample(210, 60, True)],
            'ends in ad': [sample(102, 0), sample(132, 30), ad(150)],
        }
        for name, evidence in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    playback_segments(self.job(evidence), 2000, evidence[-1]['observed_at'] + 1)

    def test_reject_muted_content_and_wrong_video(self):
        for key, value in (('muted', True), ('video_id', 'other_id123'), ('rate', 2)):
            with self.subTest(key=key):
                evidence = [sample(102, 0), sample(132, 30), sample(162, 60, True)]
                evidence[1][key] = value
                with self.assertRaises(ValueError):
                    playback_segments(self.job(evidence), 67, 167)


if __name__ == '__main__':
    unittest.main()

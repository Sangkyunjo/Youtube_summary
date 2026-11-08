import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from browser_transcript import parse_segment
from realtime_fallback import PlaybackQueue
from transcript_acquisition import UI_SOURCE, from_job, review_summary

VID = 'abcdefghijk'


def document():
    return dict(video_id=VID, source=UI_SOURCE, complete=True, captured_at=time.time(),
                language='en', generated=False,
                segments=[{'start_seconds': 0, 'text': 'Caption source text. ' * 15},
                          {'start_seconds': 50, 'text': 'The final spoken sentence.'}])


class ImportAndMigrationTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.queue = PlaybackQueue(self.root / 'queue.sqlite3')
        self.queue.enqueue({'video_id': VID}, 'captions_blocked', 'narrative')

    def test_import_preserves_source_timing_and_does_not_fake_a_recording_or_delivery(self):
        job = self.queue.import_ui(VID, document())
        self.assertEqual(job['status'], 'ready')
        self.assertEqual(job['next_action'], 'deliver')
        self.assertEqual(job['evidence'], [])
        self.assertIsNone(job['started'])
        self.assertTrue(self.queue.needs_delivery(VID, 'narrative'))
        source = from_job(job)
        self.assertEqual(source['source'], UI_SOURCE)
        self.assertEqual(source['language'], 'en')
        self.assertFalse(source['generated'])
        self.assertEqual(source['segments'][1]['start'], 50)
        self.assertNotIn('duration', source['segments'][1])

    def test_bad_or_partial_ui_input_keeps_job_retryable(self):
        for edit in ({'video_id': 'bcdefghijkl'}, {'complete': False}, {'segments': []},
                     {'captured_at': float('nan')}, {'generated': 'yes'},
                     {'segments': [{'start_seconds': 20, 'text': 'text ' * 60},
                                   {'start_seconds': 1, 'text': 'out of order'}]}):
            value = dict(document(), **edit)
            with self.subTest(edit=edit), self.assertRaises(ValueError):
                self.queue.import_ui(VID, value)
            self.assertEqual(self.queue.get(VID)['status'], 'pending')

    def test_a_live_recording_or_completed_transcript_cannot_be_overwritten_by_ui(self):
        self.queue.claim(VID)
        with self.assertRaises(ValueError):
            self.queue.import_ui(VID, document())
        self.queue.finish(VID, 'original audio text ' * 20, 'local-model')
        with self.assertRaises(ValueError):
            self.queue.import_ui(VID, document())
        self.assertEqual(self.queue.get(VID)['acquisition']['source'], 'local_whisper')

    def test_daily_delivery_is_registered_for_a_narrative_only_job(self):
        self.queue.import_ui(VID, document())
        self.queue.delivered(VID, 'daily', 'summary.txt')
        self.assertFalse(self.queue.needs_delivery(VID, 'daily'))
        self.assertTrue(self.queue.needs_delivery(VID, 'narrative'))

    def test_ui_failure_advances_to_playback_without_marking_processed(self):
        self.queue.ui_unavailable(VID, 'ui_incomplete_or_loading')
        job = self.queue.get(VID)
        self.assertEqual(job['status'], 'pending')
        self.assertEqual(job['next_action'], 'playback')
        self.assertIsNone(job['transcript'])

    def test_old_database_is_migrated_without_losing_ready_text_or_destinations(self):
        path = self.root / 'old.sqlite3'
        with sqlite3.connect(path) as db:
            db.execute('''CREATE TABLE jobs(video_id TEXT PRIMARY KEY,payload TEXT NOT NULL,
                reason TEXT NOT NULL,status TEXT NOT NULL,transcript TEXT,started REAL,
                updated REAL NOT NULL,error TEXT,evidence TEXT NOT NULL,model TEXT)''')
            db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (VID, json.dumps({'video_id': VID}), 'blocked', 'ready', 'old text',
                        None, 1, None, '[]', 'youtube-captions'))
            db.execute('CREATE TABLE deliveries(video_id TEXT,destination TEXT,output TEXT,PRIMARY KEY(video_id,destination))')
            db.execute('INSERT INTO deliveries VALUES(?,?,?)', (VID, 'daily', 'old.txt'))
        migrated = PlaybackQueue(path)
        job = migrated.get(VID)
        self.assertEqual(job['transcript'], 'old text')
        self.assertEqual(job['next_action'], 'deliver')
        self.assertFalse(migrated.needs_delivery(VID, 'daily'))
        self.assertEqual(from_job(job)['source'], 'youtube_captions_legacy')
        self.assertIsNone(from_job(job)['language'])
        PlaybackQueue(path)  # Migration is idempotent.


class ReviewAndUiParsingTest(unittest.TestCase):
    def test_caption_timestamp_formats_and_multiline_text(self):
        self.assertEqual(parse_segment('1:05\nCaption text'),
                         {'start_seconds': 65, 'text': 'Caption text'})
        self.assertEqual(parse_segment('1:02:03 text')['start_seconds'], 3723)
        self.assertIsNone(parse_segment('1:70 impossible'))
        self.assertIsNone(parse_segment('Search transcript'))

    def test_generated_paraphrase_is_flagged_instead_of_certified_as_a_quote(self):
        result = review_summary('The original speech.', {'quotes': '- "Invented quotation"'})
        self.assertEqual(result['status'], 'pending')
        self.assertTrue(result['required'])
        self.assertIn('quote_not_verbatim', result['flags'])
        self.assertNotIn('quote_not_verbatim', review_summary(
            'The original speech.', {'quotes': '"The original speech."'})['flags'])

import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import Mock, patch

import main
import queue_consumer as qc
from channel_monitor import VideoInfo
from realtime_fallback import PlaybackQueue
from test_transcript_acquisition import document

VID = 'abcdefghijk'


class IntegrationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.queue = PlaybackQueue(self.root / 'realtime.sqlite3')
        self.video = VideoInfo(VID, 'Test video', 'Test channel', 'UCtest',
                               datetime.now(timezone.utc), f'https://www.youtube.com/watch?v={VID}')
        self.system = main.YouTubeSummarySystem.__new__(main.YouTubeSummarySystem)
        self.system.playback = self.queue
        self.system._captions_blocked = False
        self.system.transcript_extractor = Mock()
        self.system.state_manager = Mock()
        self.system.summarizer = Mock()

    def test_daily_block_defers_and_does_not_mark_processed_or_call_llm(self):
        self.system.transcript_extractor.extract_with_status.return_value = (None, 'blocked')
        result = self.system.process_video(self.video)
        self.assertEqual(result['error'], 'Awaiting realtime playback')
        self.assertEqual(self.queue.get(VID)['status'], 'pending')
        self.system.state_manager.mark_video_processed.assert_not_called()
        self.system.summarizer.summarize.assert_not_called()
        self.system.process_video(self.video)
        self.system.transcript_extractor.extract_with_status.assert_called_once()

    def test_daily_waiting_job_retries_captions_on_a_new_run(self):
        self.queue.enqueue({'video_id': VID}, 'captions_blocked', 'daily')
        self.system.transcript_extractor.extract_with_status.return_value = ('caption text ' * 20, 'ok')
        self.system.summarizer.summarize.return_value = {'overview': 'Summary'}
        self.system.summarizer.create_summary_text.return_value = 'Summary text'
        with patch.object(main, 'get_summaries_dir', return_value=self.root):
            result = self.system.process_video(self.video)
        self.assertTrue(result['success'])
        self.assertEqual(self.queue.get(VID)['status'], 'ready')
        self.assertEqual(self.queue.waiting('daily'), [])

    def test_resume_retries_waiting_jobs_and_stops_at_a_block(self):
        published_at = datetime.now(timezone.utc).isoformat()
        for vid in (VID, 'bcdefghijkl'):
            self.queue.enqueue(dict(video_id=vid, title='Title', channel_title='Channel',
                                    published_at=published_at, url='https://youtu.be/x'),
                               'captions_blocked', 'daily')
        self.system.transcript_extractor.extract_with_status.return_value = (None, 'blocked')
        with patch.object(qc, '_REQUEST_DELAY', 0):
            results = self.system.resume_realtime()
        self.assertEqual(len(results), 1)
        self.system.transcript_extractor.extract_with_status.assert_called_once()
        self.assertEqual(len(self.queue.waiting('daily')), 2)

    def test_ready_local_transcript_resumes_without_caption_request(self):
        self.queue.enqueue({'video_id': VID}, 'blocked')
        self.queue.claim(VID)
        self.queue.finish(VID, 'real local transcript ' * 20, 'local')
        self.system.summarizer.summarize.return_value = {'overview': 'Summary'}
        self.system.summarizer.create_summary_text.return_value = 'Summary text'
        with patch.object(main, 'get_summaries_dir', return_value=self.root):
            result = self.system.process_video(self.video)
        self.assertTrue(result['success'])
        self.assertTrue(Path(result['summary_path']).exists())
        self.system.transcript_extractor.extract_with_status.assert_not_called()
        self.system.state_manager.mark_video_processed.assert_called_once()
        self.assertEqual(self.queue.ready('daily'), [])

    def test_ui_transcript_uses_existing_summary_pipeline_and_persists_review_metadata(self):
        import json
        self.queue.enqueue({'video_id': VID}, 'blocked', 'narrative')
        self.queue.import_ui(VID, document())
        self.system.summarizer.summarize.return_value = {'overview': 'Summary', 'quotes': '"Invented quote"'}
        self.system.summarizer.create_summary_text.return_value = 'Summary text'
        with patch.object(main, 'get_summaries_dir', return_value=self.root):
            result = self.system.process_video(self.video)
        self.assertTrue(result['success'])
        self.system.transcript_extractor.extract_with_status.assert_not_called()
        metadata = json.loads(Path(result['metadata_path']).read_text())
        self.assertEqual(metadata['acquisition']['source'], 'youtube_visible_transcript')
        self.assertEqual(metadata['acquisition']['language'], 'en')
        self.assertEqual(metadata['review']['status'], 'pending')
        self.assertIn('quote_not_verbatim', metadata['review']['flags'])
        self.assertFalse(self.queue.needs_delivery(VID, 'daily'))
        self.assertTrue(self.queue.needs_delivery(VID, 'narrative'))

    def test_queue_retains_deferred_rows_without_paid_stt_or_success_marker(self):
        qdir = self.root / 'pending'; qdir.mkdir()
        infile = qdir / 'pending_test.jsonl'; infile.write_text('placeholder')
        state = Mock(); state.is_video_processed.return_value = False
        captions = Mock(); captions.extract_with_status.return_value = (None, 'blocked')
        row = dict(video_id=VID, title='Title', published_at=datetime.now(timezone.utc).isoformat())
        with (patch.object(qc, '_queue_dir', return_value=qdir),
              patch.object(qc, '_inbox_dir', return_value=self.root),
              patch.object(qc, '_read_queue', return_value=[row]),
              patch.object(qc, 'StateManager', return_value=state),
              patch.object(qc, '_TRANSCRIBER', 'captions-first'),
              patch.object(qc, '_REQUEST_DELAY', 0),
              patch.object(qc, 'realtime_enabled', return_value=True),
              patch.object(qc, 'PlaybackQueue', return_value=self.queue),
              patch.object(qc, '_make_caption_transcriber', return_value=captions),
              patch.object(qc, '_make_whisper_transcriber', side_effect=AssertionError('Paid STT forbidden')),
              patch.object(qc, 'Summarizer', side_effect=AssertionError('No transcript'))):
            result = qc.run()
        self.assertEqual(result['deferred'], 1)
        self.assertEqual(result['written'], 0)
        self.assertTrue(infile.exists())
        state.mark_video_processed.assert_not_called()


if __name__ == '__main__':
    unittest.main()

class QueueResumeTest(unittest.TestCase):
    def test_ui_transcript_delivers_without_rediscovery_files_or_new_caption_request(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue = PlaybackQueue(root / 'realtime.sqlite3')
            queue.enqueue(dict(video_id=VID, title='Title', channel_title='Channel',
                               published_at=datetime.now(timezone.utc).isoformat()),
                          'blocked', 'narrative')
            queue.import_ui(VID, document())
            state = Mock(); state.is_video_processed.return_value = True
            captions = Mock(engine='youtube-transcript-api')
            summarizer = Mock()
            summarizer.summarize.return_value = {'overview': 'UI source summary'}
            with (patch.object(qc, '_queue_dir', return_value=root),
                  patch.object(qc, '_inbox_dir', return_value=root),
                  patch.object(qc, '_read_queue', return_value=[]),
                  patch.object(qc, 'StateManager', return_value=state),
                  patch.object(qc, '_TRANSCRIBER', 'captions-first'),
                  patch.object(qc, 'realtime_enabled', return_value=True),
                  patch.object(qc, 'PlaybackQueue', return_value=queue),
                  patch.object(qc, '_make_caption_transcriber', return_value=captions),
                  patch.object(qc, 'Summarizer', return_value=summarizer)):
                result = qc.run(only={VID})
                self.assertEqual(qc.run(only={VID})['written'], 0)
            self.assertEqual(result['written'], 1)
            self.assertEqual(result['ui_used'], 1)
            captions.extract_with_status.assert_not_called()
            doc = json.loads((root / f'{VID}.json').read_text())
            self.assertEqual(doc['stt']['engine'], 'youtube-visible-transcript')
            self.assertEqual(doc['stt']['lang'], 'en')
            self.assertEqual(doc['stt']['acquisition'], 'captions_ui')
            self.assertEqual(doc['summary_meta']['review']['status'], 'pending')
            self.assertNotIn('duration', doc['transcript_evidence']['segments'][0])
            self.assertFalse(queue.needs_delivery(VID, 'narrative'))

    def test_daily_completion_does_not_drop_narrative_delivery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue = PlaybackQueue(root / 'realtime.sqlite3')
            row = dict(video_id=VID, title='Title', channel_title='Channel',
                       published_at=datetime.now(timezone.utc).isoformat())
            queue.enqueue(row, 'blocked', 'daily')
            queue.claim(VID)
            queue.finish(VID, 'local transcript ' * 30, 'local-model')
            queue.delivered(VID, 'daily', 'existing-summary.txt')
            state = Mock(); state.is_video_processed.return_value = True
            captions = Mock(engine='youtube-transcript-api', model=None, last_track_info=None)
            summarizer = Mock()
            summarizer.summarize.return_value = {'overview': 'Verified narrative summary'}
            with (patch.object(qc, '_queue_dir', return_value=root),
                  patch.object(qc, '_inbox_dir', return_value=root),
                  patch.object(qc, '_read_queue', return_value=[row]),
                  patch.object(qc, 'StateManager', return_value=state),
                  patch.object(qc, '_TRANSCRIBER', 'captions-first'),
                  patch.object(qc, '_REQUEST_DELAY', 0),
                  patch.object(qc, 'realtime_enabled', return_value=True),
                  patch.object(qc, 'PlaybackQueue', return_value=queue),
                  patch.object(qc, '_make_caption_transcriber', return_value=captions),
                  patch.object(qc, 'Summarizer', return_value=summarizer)):
                result = qc.run()
            self.assertEqual(result['written'], 1)
            self.assertEqual(result['realtime_used'], 1)
            captions.extract_with_status.assert_not_called()
            import json
            output = json.loads((root / f'{VID}.json').read_text())
            self.assertEqual(output['stt']['acquisition'], 'realtime_playback')
            self.assertFalse(queue.needs_delivery(VID, 'narrative'))

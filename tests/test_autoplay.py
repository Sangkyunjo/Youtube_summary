import unittest
from unittest.mock import Mock, patch

import autoplay
from autoplay import CAPTURE_OUTPUT, AudioRoute

PROFILE = """Audio:

    Devices:

        Mac mini 스피커:

          Default Output Device: Yes
          Default System Output Device: Yes
          Output Channels: 2

        Fanding Capture:

          Output Channels: 2
"""


class RefusalTest(unittest.TestCase):
    def test_player_error_screen_stops_the_session_without_workaround(self):
        error = autoplay.refused('player_error')
        self.assertIsInstance(error, autoplay.PlaybackStopped)
        self.assertIn('오류 화면', str(error))
        self.assertIn('우회하지 않습니다', str(error))


class AudioRouteTest(unittest.TestCase):
    def test_default_output_is_read_from_system_profiler(self):
        result = type('Result', (), {'stdout': PROFILE})()
        with patch.object(autoplay.subprocess, 'run', return_value=result):
            self.assertEqual(autoplay.default_output(), 'Mac mini 스피커')

    def test_switches_for_the_recording_and_restores_even_on_failure(self):
        calls = []
        with (patch.object(autoplay, 'default_output', return_value='Mac mini 스피커'),
              patch.object(autoplay, 'set_output', side_effect=calls.append)):
            with self.assertRaises(RuntimeError):
                with AudioRoute():
                    self.assertEqual(calls, [CAPTURE_OUTPUT])
                    raise RuntimeError('recorder failed')
        self.assertEqual(calls, [CAPTURE_OUTPUT, 'Mac mini 스피커'])

    def test_leaves_output_alone_when_it_already_feeds_the_capture(self):
        calls = []
        with (patch.object(autoplay, 'default_output', return_value=CAPTURE_OUTPUT),
              patch.object(autoplay, 'set_output', side_effect=calls.append)):
            with AudioRoute():
                pass
        self.assertEqual(calls, [])


class UiFirstTest(unittest.TestCase):
    def player(self, job):
        queue = Mock()
        queue.get.return_value = job
        with patch.object(autoplay, 'load_config', return_value={}):
            player = autoplay.AutoPlayer(queue)
        page = Mock()
        player._context = Mock(pages=[page])
        player.ui_reader = Mock()
        return player, page, queue

    def test_caption_success_never_starts_a_recorder(self):
        player, page, queue = self.player({'status': 'pending', 'next_action': 'ui_transcript'})
        doc = {'complete': True}
        player.ui_reader.read.return_value = doc, 'ok'
        queue.import_ui.return_value = {'transcript': 'captions ' * 40}
        with patch.object(autoplay, 'Recorder', side_effect=AssertionError('No recording')):
            result = player.play({'video_id': 'abcdefghijk', 'duration_seconds': 60})
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(result['acquisition'], 'youtube_visible_transcript')
        queue.import_ui.assert_called_once_with('abcdefghijk', doc)

    def test_ui_loading_defers_to_regular_browser_without_opening_a_recorder(self):
        player, page, queue = self.player({'status': 'pending', 'next_action': 'ui_transcript'})
        player.ui_reader.read.return_value = None, 'ui_incomplete_or_loading'
        with patch.object(autoplay, 'Recorder', side_effect=AssertionError('No recording')):
            result = player.play({'video_id': 'abcdefghijk', 'duration_seconds': 60})
        self.assertEqual(result['status'], 'deferred')
        queue.ui_unavailable.assert_called_once_with('abcdefghijk', 'ui_incomplete_or_loading')

    def test_user_stop_is_preserved_without_advancing_to_playback(self):
        player, page, queue = self.player({'status': 'pending', 'next_action': 'ui_transcript'})
        player.ui_reader.read.return_value = None, 'stopped'
        self.assertEqual(player.play({'video_id': 'abcdefghijk'})['status'], 'stopped')
        queue.ui_unavailable.assert_not_called()


if __name__ == '__main__':
    unittest.main()

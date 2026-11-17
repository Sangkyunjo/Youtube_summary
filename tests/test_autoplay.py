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

    def route(self, levels, previous='Mac mini 스피커'):
        calls, released = [], []
        patches = (patch.object(autoplay, 'default_output', return_value=previous),
                   patch.object(autoplay, 'set_output', side_effect=calls.append),
                   patch.object(autoplay, 'capture_lock', return_value=lambda: released.append(True)),
                   patch.object(autoplay, 'loopback_level', side_effect=lambda out, _in: levels[out]))
        return patches, calls, released

    def test_uses_the_first_route_that_records_the_tone_and_restores_the_output(self):
        patches, calls, released = self.route({CAPTURE_OUTPUT: -70.0, 'BlackHole 2ch': -38.0})
        with patches[0], patches[1], patches[2], patches[3]:
            with self.assertRaises(RuntimeError):
                with AudioRoute() as route:
                    self.assertEqual((route.output, route.input), ('BlackHole 2ch', 'BlackHole 2ch'))
                    self.assertEqual(released, [True])
                    raise RuntimeError('recorder failed')
        self.assertEqual(calls, ['Mac mini 스피커'])

    def test_no_working_route_stops_the_session_and_restores_the_output(self):
        patches, calls, released = self.route({CAPTURE_OUTPUT: None, 'BlackHole 2ch': -58.0})
        with patches[0], patches[1], patches[2], patches[3]:
            with self.assertRaises(autoplay.PlaybackStopped) as stopped:
                AudioRoute().__enter__()
        self.assertIn('소리 경로', str(stopped.exception))
        self.assertEqual(calls, ['Mac mini 스피커'])
        self.assertEqual(released, [True])

    def test_leaves_output_alone_when_it_already_is_the_verified_route(self):
        patches, calls, _released = self.route({CAPTURE_OUTPUT: -38.0}, previous=CAPTURE_OUTPUT)
        with patches[0], patches[1], patches[2], patches[3]:
            with AudioRoute():
                pass
        self.assertEqual(calls, [])

    def test_held_capture_lock_is_busy_not_a_failure(self):
        with patch.object(autoplay, 'capture_lock', side_effect=autoplay.CaptureBusy('active')):
            with self.assertRaises(autoplay.CaptureBusy):
                AudioRoute().__enter__()


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


class PreRollTest(unittest.TestCase):
    def test_paused_ad_is_resumed_until_the_content_shows(self):
        with patch.object(autoplay, 'load_config', return_value={}):
            player = autoplay.AutoPlayer(Mock())
        ad = {'video_id': 'abcdefghijk', 'ad': True, 'duration': 15.0, 'paused': True}
        content = {'video_id': 'abcdefghijk', 'ad': False, 'duration': 1996.0, 'paused': True}
        scripts = []

        def evaluate(script):
            scripts.append(script)
            if script == autoplay.SNAPSHOT_JS:
                return content if scripts.count(autoplay.RESUME_AD_JS) >= 2 else ad
            return True
        page = Mock(evaluate=evaluate)
        page.locator.return_value.count.return_value = 0
        with patch.object(autoplay.time, 'sleep'):
            state = player._finish_pre_roll(page, 'abcdefghijk')
        self.assertFalse(state['ad'])
        self.assertEqual(scripts.count(autoplay.RESUME_AD_JS), 2)


if __name__ == '__main__':
    unittest.main()

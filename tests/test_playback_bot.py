import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import playback_bot
from autoplay import PlaybackStopped
from playback_bot import KST, PlaybackBot, Store, parse_selection


def item(number, seconds=600, published='2026-11-05T06:00:00Z'):
    return {'video_id': f'video{number:06d}', 'title': f'영상 {number}', 'channel_title': '채널',
            'channel_id': 'UC', 'published_at': published, 'url': f'https://youtu.be/video{number:06d}',
            'duration_seconds': seconds}


class FakeTelegram:
    def __init__(self):
        self.inbox, self.sent, self.next_id = [], [], 1

    def push(self, text):
        self.inbox.append((self.next_id, text))
        self.next_id += 1

    def latest_offset(self):
        return self.next_id

    def messages(self, offset, timeout):
        found = [m for m in self.inbox if m[0] >= offset]
        self.inbox = []
        return found

    def send(self, text):
        self.sent.append(text)


class ParseSelectionTest(unittest.TestCase):
    def test_numbers_ranges_all_and_none(self):
        self.assertEqual(parse_selection('3, 1 5-6', 8), [3, 1, 5, 6])
        self.assertEqual(parse_selection('2~3,2', 8), [2, 3])
        self.assertEqual(parse_selection('전체', 3), [1, 2, 3])
        self.assertEqual(parse_selection('없음', 3), [])
        for text in ('9', '0', '3-1', '안녕', ''):
            with self.subTest(text=text):
                self.assertIsNone(parse_selection(text, 8))


class PlaybackBotTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.telegram = FakeTelegram()
        self.store = Store(Path(tmp.name) / 'bot.sqlite3')
        self.store.set('offset', 1)
        self.at(2026, 11, 6, 7, 59)
        self.items = [item(1, 7200), item(2, 7200), item(3, 7200)]
        self.played, self.processed, self.outcomes = [], set(), []
        self.bot = PlaybackBot(
            self.telegram, self.store,
            fetch_candidates=lambda: {'in_window': 40, 'items': list(self.items)},
            play=self.play, deliver=lambda vid: {'narrative_written': 1, 'daily_written': 1, 'errors': 0},
            is_processed=lambda vid: vid in self.processed, clock=lambda: self.now)

    def at(self, *parts):
        self.now = datetime(*parts, tzinfo=KST).timestamp()

    def play(self, chosen, stop_requested):
        self.played.append(chosen['video_id'])
        if self.outcomes:
            outcome = self.outcomes.pop(0)
            if callable(outcome):
                return outcome(stop_requested)
            return outcome
        self.now += chosen['duration_seconds']
        return {'status': 'ready', 'chars': 1000}

    def reply(self, text):
        self.telegram.push(text)
        self.bot.step(poll_timeout=0)

    def test_offer_is_sent_once_a_day_from_eight(self):
        self.bot.step(poll_timeout=0)
        self.assertEqual(self.telegram.sent, [])
        self.at(2026, 11, 6, 8, 0)
        self.bot.step(poll_timeout=0)
        self.bot.step(poll_timeout=0)
        self.assertEqual(len(self.telegram.sent), 1)
        self.assertIn('1. [채널] 영상 1 · 120분', self.telegram.sent[0])
        self.assertIn('기한 내 미처리 40건', self.telegram.sent[0])

    def test_selection_waits_for_the_window_then_plays_in_order(self):
        self.at(2026, 11, 6, 8, 0)
        self.bot.step(poll_timeout=0)
        self.items = [item(1), item(2)]
        self.store.set('offer', {'date': '2026-11-06', 'items': self.items, 'in_window': 2})
        self.reply('2,1')
        self.assertEqual(self.played, [])
        self.assertIn('09:00부터 시작합니다', self.telegram.sent[-1])
        self.at(2026, 11, 6, 9, 0)
        for _ in range(3):
            self.bot.step(poll_timeout=0)
        self.assertEqual(self.played, ['video000002', 'video000001'])
        self.assertIn('선택한 영상 처리를 마쳤습니다. 완료 2건', self.telegram.sent[-1])
        self.assertIsNone(self.store.get('session'))

    def test_priority_channel_precedes_carryover_and_other_selected_videos(self):
        self.store.set('carry', [item(1)])
        self.items = [item(2), dict(item(3), channel_id='UCpriority')]
        self.at(2026, 11, 6, 8, 0)
        config = {'youtube': {'priority_channel_ids': ['UCpriority']}}
        with patch.object(playback_bot, 'load_config', return_value=config):
            self.bot.step(poll_timeout=0)
            offer = self.store.get('offer')
            self.assertEqual([entry['video_id'] for entry in offer['items']],
                             ['video000003', 'video000001', 'video000002'])
            self.reply('3,1,2')
            self.at(2026, 11, 6, 9, 0)
            for _ in range(3):
                self.bot.step(poll_timeout=0)
        self.assertEqual(self.played, ['video000003', 'video000002', 'video000001'])

    def test_asks_after_three_hours_and_carries_leftovers_to_next_offer(self):
        self.at(2026, 11, 6, 9, 0)
        self.bot.step(poll_timeout=0)
        self.reply('전체')
        self.bot.step(poll_timeout=0)
        self.assertEqual(self.played, ['video000001', 'video000002'])
        self.bot.step(poll_timeout=0)
        self.assertIn('3시간 더 진행할까요?', self.telegram.sent[-1])
        self.bot.step(poll_timeout=0)
        self.assertEqual(len(self.played), 2)
        self.reply('중단')
        self.assertIn('남은 1건은 내일 08:00 목록에', self.telegram.sent[-1])
        self.items = [item(4)]
        self.at(2026, 11, 7, 8, 0)
        self.bot.step(poll_timeout=0)
        offer = self.store.get('offer')
        self.assertEqual([i['video_id'] for i in offer['items']], ['video000003', 'video000004'])
        self.assertIn('이전 목록에서 이월 1건', self.telegram.sent[-1])

    def test_continue_adds_three_more_hours(self):
        self.at(2026, 11, 6, 9, 0)
        self.bot.step(poll_timeout=0)
        self.reply('전체')
        self.bot.step(poll_timeout=0)
        self.bot.step(poll_timeout=0)
        self.reply('계속')
        self.bot.step(poll_timeout=0)
        self.assertEqual(len(self.played), 3)
        self.assertIn('3시간 더 진행합니다', ''.join(self.telegram.sent))

    def test_stops_when_the_next_video_cannot_end_before_23(self):
        self.at(2026, 11, 6, 22, 0)
        self.bot.step(poll_timeout=0)
        self.store.set('offer', {'date': '2026-11-06', 'items': [item(1, 3600)], 'in_window': 1})
        self.reply('1')
        self.assertEqual(self.played, [])
        self.assertIn('23:00 전에 끝낼 수 없어', self.telegram.sent[-1])
        self.assertEqual(len(self.store.get('carry')), 1)

    def test_stop_message_during_playback_interrupts_the_video(self):
        self.at(2026, 11, 6, 9, 0)
        self.bot.step(poll_timeout=0)

        def interrupted_play(stop_requested):
            self.telegram.push('중단')
            for _ in range(5):
                self.now += 20
                if stop_requested():
                    return {'status': 'stopped'}
            return {'status': 'ready'}

        # The selection is handled and the first video starts in the same step.
        self.outcomes.append(interrupted_play)
        self.reply('1')
        self.assertIsNone(self.store.get('session'))
        self.assertEqual([i['video_id'] for i in self.store.get('carry')], ['video000001'])

    def test_busy_recorder_retries_later_without_dropping_the_video(self):
        self.at(2026, 11, 6, 9, 0)
        self.bot.step(poll_timeout=0)
        self.outcomes.append({'status': 'busy'})
        self.reply('1')
        self.bot.step(poll_timeout=0)
        self.assertEqual(self.played, ['video000001'])
        self.now += 601
        self.bot.step(poll_timeout=0)
        self.assertEqual(self.played, ['video000001', 'video000001'])

    def test_bot_check_ends_the_session_and_keeps_the_video(self):
        self.at(2026, 11, 6, 9, 0)
        self.bot.step(poll_timeout=0)

        def blocked(_stop):
            raise PlaybackStopped('YouTube asked for a bot check')

        self.outcomes.append(blocked)
        self.reply('1')
        self.assertIn('재생을 멈췄습니다: YouTube asked for a bot check', self.telegram.sent[-1])
        self.assertEqual(len(self.store.get('carry')), 1)

    def test_supervised_playback_is_deferred_and_carried_not_failed_or_delivered(self):
        self.at(2026, 11, 6, 9, 0)
        self.bot.step(poll_timeout=0)
        self.outcomes.append({'status': 'deferred', 'next_action': 'playback'})
        self.reply('1')
        session = self.store.get('session')
        self.assertEqual(session['failed'], [])
        self.assertEqual(session['done'], [])
        self.assertEqual(session['deferred'], ['video000001'])
        self.assertEqual(self.store.get('carry')[0]['video_id'], 'video000001')
        self.bot.step(poll_timeout=0)
        self.assertIn('재생·전사 대기 1건', self.telegram.sent[-1])

    def test_playback_request_is_reported_and_not_carried(self):
        self.at(2026, 11, 6, 9, 0)
        self.bot.step(poll_timeout=0)
        self.outcomes.append({'status': 'requested'})
        self.reply('1')
        session = self.store.get('session')
        self.assertEqual(session['requested'], ['video000001'])
        self.assertIn('재생 요청: [채널] 영상 1', '\n'.join(self.telegram.sent))
        self.assertIn(playback_bot.PLAYBACK_RUN_TIME, '\n'.join(self.telegram.sent))
        self.assertEqual(self.store.get('carry') or [], [])

    def test_keyword_and_expired_carryover_is_dropped_like_the_queue(self):
        keyword = dict(item(8), discovery={'mode': 'keyword', 'query': 'HBM'})
        expired = item(9, published='2020-01-01T00:00:00Z')
        self.store.set('carry', [keyword, expired, item(7)])
        with patch.object(playback_bot, 'load_config',
                          return_value={'narrative_queue': {'excluded_discovery_modes': ['keyword']}}):
            self.at(2026, 11, 6, 8, 0)
            self.bot.step(poll_timeout=0)
        offered = [entry['video_id'] for entry in self.store.get('offer')['items']]
        self.assertIn('video000007', offered)
        self.assertNotIn('video000008', offered)
        self.assertNotIn('video000009', offered)

    def test_settings_are_read_without_other_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / '.env'
            env.write_text('OTHER=1\nTELEGRAM_HERMES_BOT_TOKEN="tok"\n'
                           'export TELEGRAM_HERMES_CHAT_ID=5\nTELEGRAM_HERMES_USER_ID=7\n', encoding='utf-8')
            self.assertEqual(playback_bot.load_settings(env), ('tok', '5', '7'))


if __name__ == '__main__':
    unittest.main()


class PlayRequestTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        from realtime_fallback import PlaybackQueue
        from test_state_manager import state_at
        self.queue = PlaybackQueue(Path(tmp.name) / 'realtime.sqlite3')
        self.state = state_at(Path(tmp.name))

    def _play(self, chosen, automated=False):
        with (patch.object(playback_bot, 'PlaybackQueue', return_value=self.queue),
              patch.object(playback_bot, 'StateManager', return_value=self.state),
              patch.object(playback_bot, 'load_config',
                           return_value={'transcript_fallback': {'automated_playback': automated}}),
              patch.object(playback_bot, 'AutoPlayer', side_effect=AssertionError('no browser'))):
            return playback_bot.play(chosen, lambda: False)

    def test_waiting_job_needing_playback_becomes_a_request_with_its_own_destinations(self):
        waiting = item(1, 140)
        self.queue.enqueue({k: waiting[k] for k in ('video_id', 'published_at')}, 'captions_blocked', 'daily')
        self.queue.ui_unavailable('video000001', 'ui_incomplete_or_loading')
        result = self._play(dict(waiting, source='waiting'))
        self.assertEqual(result, {'status': 'requested'})
        self.assertIsNone(self.queue.delivery_output('video000001', 'narrative'))
        self.assertFalse(self.queue.needs_delivery('video000001', 'narrative'))
        requested = self.queue.requested_jobs()
        self.assertEqual([job['video_id'] for job in requested], ['video000001'])
        self.assertEqual(requested[0]['requested']['duration_seconds'], 140)

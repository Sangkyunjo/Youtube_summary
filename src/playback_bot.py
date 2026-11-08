"""Daily Telegram offer and selection loop for automated YouTube playback (host only).

At 08:00 KST the conversation bot sends the newest in-window queue videos. The user
answers with numbers, and the chosen videos play one at a time through
autoplay.AutoPlayer, then are summarized into the NarrativeRadar inbox and
summaries/. Every three hours of processing the bot asks whether to go on. Videos
left over return at the top of the next morning's offer. Playback stays inside the
09:00-23:00 KST collection window.
"""
import argparse
import http.client
import json
import logging
import re
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))

from autoplay import AutoPlayer, PlaybackStopped
from realtime_fallback import PlaybackQueue, get_project_root, load_config

KST = ZoneInfo('Asia/Seoul')
OFFER_HOUR = 8
WINDOW_START, WINDOW_END = 9, 23
BUDGET_SECONDS = 3 * 3600
OFFER_SIZE = 30
MAX_AGE_DAYS = 30
STOP_POLL_SECONDS = 15
CONTAINER = 'devenv-airflow-scheduler'
ENV_FILE = Path.home() / 'PersonalAgent' / '.env'
CONTINUE_WORDS = {'계속', '예', '네', '응', '진행', 'yes', 'y'}
STOP_WORDS = {'중단', '그만', '정지', '아니오', '아니요', 'stop', 'no', 'n'}
STATUS_WORDS = {'상태', 'status'}
LIST_WORDS = {'목록', 'list'}
HELP = ("번호로 답해 주세요. 예: 1,3,5-8 · '전체' · '없음'\n"
        "그 밖에 '상태', '목록', '중단'을 쓸 수 있습니다.")

logger = logging.getLogger('playback_bot')


def load_settings(path=ENV_FILE):
    """The conversation bot's token, chat and user, read from PersonalAgent's .env."""
    values = {}
    for line in Path(path).read_text(encoding='utf-8').splitlines():
        key, sep, value = line.partition('=')
        key = key.strip().removeprefix('export ').strip()
        if sep and key.startswith('TELEGRAM_HERMES_'):
            values[key] = value.strip().strip('"\'')
    names = ('TELEGRAM_HERMES_BOT_TOKEN', 'TELEGRAM_HERMES_CHAT_ID', 'TELEGRAM_HERMES_USER_ID')
    missing = [name for name in names if not values.get(name)]
    if missing:
        raise SystemExit(f'Missing in {path}: {", ".join(missing)}')
    return tuple(values[name] for name in names)


class Telegram:
    def __init__(self, token, chat_id, user_id, timeout=40):
        self._base = f'https://api.telegram.org/bot{token}'
        self.chat_id, self.user_id, self.timeout = str(chat_id), str(user_id), timeout

    def send(self, text):
        for start in range(0, len(text), 3500):
            self._call('sendMessage', {'chat_id': self.chat_id, 'text': text[start:start + 3500]})

    def latest_offset(self):
        """Offset just past whatever is already waiting, so old messages are not replayed."""
        updates = self._call('getUpdates', {'offset': '-1', 'timeout': '0'})
        return (int(updates[-1]['update_id']) + 1) if updates else 0

    def messages(self, offset, timeout):
        """[(update_id, text)] from the allowed user in the allowed chat."""
        updates = self._call('getUpdates', {'offset': str(offset), 'timeout': str(timeout),
                                            'allowed_updates': json.dumps(['message'])},
                             timeout=timeout + 10)
        found = []
        for update in updates or []:
            message = update.get('message') or {}
            chat_ok = str((message.get('chat') or {}).get('id')) == self.chat_id
            user_ok = str((message.get('from') or {}).get('id')) == self.user_id
            if not (chat_ok and user_ok and message.get('text')):
                # Why it was ignored, without the content or the ids.
                kinds = sorted(set(message) - {'message_id', 'from', 'chat', 'date', 'text'})
                logger.info('ignored update %s: chat_ok=%s user_ok=%s has_text=%s kinds=%s',
                            update.get('update_id'), chat_ok, user_ok, bool(message.get('text')), kinds[:6])
            found.append((int(update['update_id']),
                          message.get('text', '') if chat_ok and user_ok else ''))
        return found

    def _call(self, method, params, timeout=None):
        request = urllib.request.Request(f'{self._base}/{method}',
                                         data=urllib.parse.urlencode(params).encode())
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                payload = json.loads(response.read().decode())
        except urllib.error.HTTPError as error:
            raise RuntimeError(f'Telegram {method} HTTP {error.code}') from None
        except urllib.error.URLError as error:
            raise RuntimeError(f'Telegram {method} network failure ({type(error.reason).__name__})') from None
        except (http.client.HTTPException, OSError) as error:
            raise RuntimeError(f'Telegram {method} network failure ({type(error).__name__})') from None
        if not payload.get('ok'):
            raise RuntimeError(f"Telegram {method} failed: {payload.get('description', 'unknown')}")
        return payload.get('result')


class Store:
    """Small JSON key-value state that survives restarts."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)')

    def get(self, key, default=None):
        with sqlite3.connect(self.path) as db:
            row = db.execute('SELECT value FROM kv WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        with sqlite3.connect(self.path) as db:
            db.execute('INSERT OR REPLACE INTO kv(key,value) VALUES(?,?)',
                       (key, json.dumps(value, ensure_ascii=False)))


def parse_selection(text, count):
    """Numbers, ranges, '전체' or '없음' → sorted unique item numbers; None if unreadable."""
    text = text.strip()
    if text in ('전체', '모두', 'all'):
        return list(range(1, count + 1))
    if text in ('없음', '안함', 'none', 'skip'):
        return []
    picked = []
    for part in re.split(r'[\s,]+', text):
        if not part:
            continue
        match = re.fullmatch(r'(\d+)(?:[-~](\d+))?', part)
        if not match:
            return None
        first, last = int(match[1]), int(match[2] or match[1])
        if not 1 <= first <= last <= count:
            return None
        picked += [n for n in range(first, last + 1) if n not in picked]
    return picked or None


def minutes(seconds):
    return max(1, round(seconds / 60))


def hours_text(seconds):
    total = round(seconds / 60)
    return f'{total // 60}시간 {total % 60}분' if total >= 60 else f'{total}분'


def format_offer(day, items, in_window, carried):
    lines = [f'YouTube 처리 후보 · {day}',
             f'기한 내 미처리 {in_window}건 중 최신순' + (f', 앞의 {len(carried)}건은 어제 남은 영상' if carried else ''), '']
    for number, item in enumerate(items, start=1):
        title = item['title'] if len(item['title']) <= 70 else item['title'][:69] + '…'
        lines.append(f"{number}. [{item['channel_title']}] {title} · {minutes(item['duration_seconds'])}분")
    total = sum(item['duration_seconds'] for item in items)
    lines += ['', f'전체 재생 시간 약 {hours_text(total)}', HELP,
              '처리 3시간마다 계속할지 묻고, 재생은 09:00~23:00에만 합니다.']
    return '\n'.join(lines)


def too_old(item, now):
    try:
        published = datetime.fromisoformat((item.get('published_at') or '').replace('Z', '+00:00'))
    except ValueError:
        return False
    if published.tzinfo is None:
        published = published.replace(tzinfo=KST)
    return published < now - timedelta(days=MAX_AGE_DAYS)


class PlaybackBot:
    def __init__(self, telegram, store, *, fetch_candidates, play, deliver, is_processed, clock=time.time):
        self.telegram, self.store = telegram, store
        self.fetch_candidates, self.play, self.deliver = fetch_candidates, play, deliver
        self.is_processed, self.clock = is_processed, clock

    def now(self):
        return datetime.fromtimestamp(self.clock(), KST)

    def serve(self):
        while True:
            try:
                self.step()
            except RuntimeError as error:
                logger.warning('step failed: %s', error)
                time.sleep(30)
            except Exception:
                logger.exception('step failed')
                time.sleep(30)

    def step(self, poll_timeout=30):
        self._maybe_offer(self.now())
        waiting = not self._runnable(self.store.get('session'))
        for update_id, text in self._fetch(poll_timeout if waiting else 0):
            logger.info('received update %s: %r', update_id, text[:40])
            if text:
                self._on_text(text)
            self.store.set('offset', update_id + 1)
        session = self.store.get('session')
        if session and not session['queue']:
            self._finish(session)
        elif self._runnable(session):
            self._run_next(session)

    # -- Telegram ---------------------------------------------------------

    def _fetch(self, timeout):
        offset = self.store.get('offset')
        if offset is None:
            offset = self.telegram.latest_offset()
            self.store.set('offset', offset)
        return self.telegram.messages(offset, timeout)

    def _say(self, text):
        try:
            self.telegram.send(text)
        except Exception:
            logger.exception('could not send a Telegram message')

    # -- daily offer --------------------------------------------------------

    def _maybe_offer(self, now):
        day = now.date().isoformat()
        if now.hour < OFFER_HOUR or self.store.get('offer_date') == day:
            return
        if self.store.get('offer_retry_at', 0) > self.clock():
            return
        try:
            offer = self.fetch_candidates()
        except Exception as error:
            logger.exception('candidate lookup failed')
            self.store.set('offer_retry_at', self.clock() + 900)
            if self.store.get('offer_error_day') != day:
                self.store.set('offer_error_day', day)
                self._say(f'오늘 후보 목록을 만들지 못했습니다. 15분 뒤 다시 시도합니다. ({error})')
            return
        session = self.store.get('session')
        carried = self.store.get('carry', []) + (session['queue'] if session else [])
        if session:
            self.store.set('session', None)
        carried = [item for item in carried if not self.is_processed(item['video_id']) and not too_old(item, now)]
        carried = list({item['video_id']: item for item in carried}.values())
        seen = {item['video_id'] for item in carried}
        items = carried + [item for item in offer['items'] if item['video_id'] not in seen][:OFFER_SIZE]
        self._say(format_offer(day, items, offer['in_window'], carried))
        logger.info('offer sent for %s: %s items, %s carried', day, len(items), len(carried))
        self.store.set('offer', {'date': day, 'items': items, 'in_window': offer['in_window']})
        self.store.set('offer_date', day)
        self.store.set('carry', [])

    # -- user messages ------------------------------------------------------

    def _on_text(self, text):
        text = text.strip()
        session = self.store.get('session')
        if text.lower() in STATUS_WORDS:
            self._say(self._status(session))
            return
        if text.lower() in LIST_WORDS:
            offer = self.store.get('offer')
            self._say(format_offer(offer['date'], offer['items'], offer['in_window'], []) if offer
                      else '아직 오늘 후보 목록이 없습니다. 08:00에 보내 드립니다.')
            return
        if session and session.get('awaiting'):
            if text.lower() in CONTINUE_WORDS:
                session['budget'] += BUDGET_SECONDS
                session['awaiting'] = False
                self.store.set('session', session)
                self._say(f"3시간 더 진행합니다. 남은 {len(session['queue'])}건.")
            elif text.lower() in STOP_WORDS:
                self._stop(session, '중단했습니다.')
            else:
                self._say("'계속' 또는 '중단'으로 답해 주세요.")
            return
        if text.lower() in STOP_WORDS:
            if session:
                self._stop(session, '중단했습니다.')
            else:
                self._say('진행 중인 작업이 없습니다.')
            return
        offer = self.store.get('offer')
        if not offer:
            self._say('아직 오늘 후보 목록이 없습니다. 08:00에 보내 드립니다.')
            return
        picked = parse_selection(text, len(offer['items']))
        if picked is None:
            self._say(HELP)
            return
        if not picked:
            self._say('오늘은 처리하지 않습니다. 내일 08:00에 다시 보내 드립니다.')
            return
        chosen = [offer['items'][number - 1] for number in picked]
        if session:
            queued = {item['video_id'] for item in session['queue']}
            session['queue'] += [item for item in chosen if item['video_id'] not in queued]
            self.store.set('session', session)
            self._say(f"{len(chosen)}건을 추가했습니다. 남은 {len(session['queue'])}건.")
            return
        session = {'queue': chosen, 'done': [], 'failed': [], 'elapsed': 0,
                   'budget': BUDGET_SECONDS, 'awaiting': False, 'retry_at': 0, 'deferred': []}
        self.store.set('session', session)
        logger.info('selected %s', [item['video_id'] for item in chosen])
        total = sum(item['duration_seconds'] for item in chosen)
        start = '' if self._in_window(self.now()) else ' 09:00부터 시작합니다.'
        self._say(f'{len(chosen)}건을 처리합니다. 재생 시간 약 {hours_text(total)}.{start}')

    # -- processing ---------------------------------------------------------

    def _in_window(self, now):
        return WINDOW_START <= now.hour < WINDOW_END

    def _runnable(self, session):
        return bool(session and session['queue'] and not session.get('awaiting')
                    and session.get('retry_at', 0) <= self.clock() and self._in_window(self.now()))

    def _run_next(self, session):
        if session['elapsed'] >= session['budget']:
            session['awaiting'] = True
            self.store.set('session', session)
            left = sum(item['duration_seconds'] for item in session['queue'])
            self._say(f"처리 시간이 {hours_text(session['elapsed'])}가 되어 멈췄습니다. "
                      f"완료 {len(session['done'])}건, 실패 {len(session['failed'])}건, "
                      f"재생·전사 대기 {len(session.get('deferred', []))}건, "
                      f"남은 {len(session['queue'])}건(재생 약 {hours_text(left)}).\n"
                      "3시간 더 진행할까요? '계속' 또는 '중단'으로 답해 주세요.")
            return
        item = session['queue'][0]
        now = self.now()
        if self.is_processed(item['video_id']) or too_old(item, now):
            session['queue'].pop(0)
            self.store.set('session', session)
            return
        window_end = now.replace(hour=WINDOW_END, minute=0, second=0, microsecond=0)
        if now + timedelta(seconds=item['duration_seconds'] * 1.15 + 300) > window_end:
            self._stop(session, f"다음 영상({minutes(item['duration_seconds'])}분)을 23:00 전에 끝낼 수 없어 "
                                '오늘은 여기까지 합니다.')
            return
        started = self.clock()
        stop = self._stop_watch()
        logger.info('playing %s (%ss)', item['video_id'], item['duration_seconds'])
        try:
            outcome = self.play(item, stop)
        except PlaybackStopped as error:
            logger.warning('session stopped on %s: %s', item['video_id'], error)
            session = self.store.get('session') or session
            session['elapsed'] += self.clock() - started
            self._stop(session, f'재생을 멈췄습니다: {error}.')
            return
        if outcome['status'] == 'ready':
            try:
                outcome = dict(outcome, **self.deliver(item['video_id']))
            except Exception as error:
                outcome = {'status': 'failed', 'error': f'summary: {error}'}
        session = self.store.get('session') or session
        session['elapsed'] += self.clock() - started
        logger.info('outcome %s %s', item['video_id'], outcome)
        if outcome['status'] == 'busy':
            session['retry_at'] = self.clock() + 600
            self.store.set('session', session)
            if not session.get('busy_told'):
                session['busy_told'] = True
                self.store.set('session', session)
                self._say('다른 녹음(강의 등)이 진행 중이라 10분 뒤 다시 시도합니다.')
            return
        if outcome['status'] == 'stopped':
            self._stop(session, '현재 영상을 멈추고 중단했습니다. 이 영상은 내일 목록에 다시 올립니다.')
            return
        session['queue'].pop(0)
        session['busy_told'] = False
        if outcome['status'] == 'ready':
            session['done'].append(item['video_id'])
            self._say(f"완료 {len(session['done'])}건: [{item['channel_title']}] {item['title'][:60]}")
        elif outcome['status'] == 'deferred':
            session.setdefault('deferred', []).append(item['video_id'])
            self.store.set('carry', self.store.get('carry', []) + [item])
            self._say(f"재생·전사 대기: [{item['channel_title']}] {item['title'][:60]} "
                      '(일반 브라우저에서 재생 후 요약을 재개합니다)')
        else:
            session['failed'].append(item['video_id'])
            self._say(f"실패: [{item['channel_title']}] {item['title'][:60]} ({outcome.get('error', '')[:160]})")
        self.store.set('session', session)

    def _stop_watch(self):
        """Callback for the player: answers '상태' and returns True once '중단' arrives."""
        state = {'polled': self.clock(), 'stop': False}

        def stop_requested():
            if state['stop'] or self.clock() - state['polled'] < STOP_POLL_SECONDS:
                return state['stop']
            state['polled'] = self.clock()
            try:
                updates = self._fetch(0)
            except Exception:
                logger.warning('Telegram poll during playback failed', exc_info=True)
                return False
            for update_id, text in updates:
                text = text.strip()
                if text.lower() in STOP_WORDS:
                    state['stop'] = True
                elif text:
                    self._on_text(text)
                self.store.set('offset', update_id + 1)
            return state['stop']

        return stop_requested

    def _stop(self, session, message):
        self.store.set('carry', self.store.get('carry', []) + session['queue'])
        self.store.set('session', None)
        self._say(f"{message} 완료 {len(session['done'])}건, 실패 {len(session['failed'])}건, "
                  f"재생·전사 대기 {len(session.get('deferred', []))}건, "
                  f"처리 시간 {hours_text(session['elapsed'])}. 남은 {len(session['queue'])}건은 "
                  '내일 08:00 목록 맨 위에 다시 올립니다.')

    def _finish(self, session):
        self.store.set('session', None)
        self._say(f"선택한 영상 처리를 마쳤습니다. 완료 {len(session['done'])}건, "
                  f"실패 {len(session['failed'])}건, 재생·전사 대기 {len(session.get('deferred', []))}건, "
                  f"처리 시간 {hours_text(session['elapsed'])}.")

    def _status(self, session):
        if not session:
            return '진행 중인 작업이 없습니다.'
        state = '계속 여부 답변 대기' if session.get('awaiting') else '처리 중'
        return (f"{state}: 완료 {len(session['done'])}건, 실패 {len(session['failed'])}건, "
                f"재생·전사 대기 {len(session.get('deferred', []))}건, "
                f"남은 {len(session['queue'])}건, 처리 시간 {hours_text(session['elapsed'])} "
                f"/ 확인 시점 {hours_text(session['budget'])}")


# -- real collaborators ------------------------------------------------------

def _container(*args, timeout):
    result = subprocess.run(['docker', 'exec', '-w', '/opt/airflow/src', CONTAINER, 'python', 'main.py', *args],
                            capture_output=True, text=True, timeout=timeout)
    lines = [line for line in result.stdout.splitlines() if line.startswith('{')]
    if not lines:
        raise RuntimeError(f'main.py {args[0]} exit {result.returncode}: {result.stderr[-300:]}')
    return json.loads(lines[-1])


def fetch_candidates():
    return _container('--candidates', str(OFFER_SIZE), timeout=300)


def deliver(video_id):
    # Zero written without errors means NAR-08 delivered the same transcript first.
    result = _container('--deliver-ready', video_id, timeout=900)
    if result['errors']:
        raise RuntimeError(f'summaries not written: {result}')
    return result


def is_processed(video_id):
    state = json.loads((get_project_root() / 'data' / 'state.json').read_text(encoding='utf-8'))
    return video_id in state.get('processed_videos', {})


def play(item, stop_requested):
    queue = PlaybackQueue()
    payload = {key: item.get(key) for key in ('video_id', 'title', 'channel_title', 'channel_id',
                                              'published_at', 'url')}
    for destination in ('narrative', 'daily'):
        queue.enqueue(payload, 'user_selected', destination)
    if queue.get(item['video_id'])['status'] == 'ready':
        return {'status': 'ready', 'chars': 0}
    if (queue.get(item['video_id'])['next_action'] == 'playback'
            and (load_config().get('transcript_fallback') or {}).get('automated_playback') is not True):
        return {'status': 'deferred', 'next_action': 'playback'}
    with AutoPlayer(queue) as player:
        return player.play(item, stop_requested)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('command', choices=('serve', 'offer'),
                        help="serve: run forever; offer: send today's offer now")
    args = parser.parse_args()
    log_dir = get_project_root() / 'logs'
    log_dir.mkdir(exist_ok=True)
    handler = RotatingFileHandler(log_dir / 'playback_bot.log', maxBytes=5_000_000, backupCount=3)
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    logging.basicConfig(level=logging.INFO, handlers=[handler, logging.StreamHandler()])
    bot = PlaybackBot(Telegram(*load_settings()), Store(get_project_root() / 'data' / 'playback_bot.sqlite3'),
                      fetch_candidates=fetch_candidates, play=play, deliver=deliver, is_processed=is_processed)
    if args.command == 'offer':
        bot.store.set('offer_date', None)
        bot.store.set('offer_retry_at', 0)
        bot._maybe_offer(bot.now().replace(hour=max(OFFER_HOUR, bot.now().hour)))
    else:
        bot.serve()


if __name__ == '__main__':
    main()

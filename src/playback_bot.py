"""Morning Telegram offer, selection loop and briefing for YouTube summaries (host only).

User request 2026-11-19: every morning the user picks which subscription videos to summarize,
then receives one briefing: the market summary, the picked videos' summaries and what is worth
remembering from the TrillionStack dashboard.

At 08:00 KST the conversation bot sends the offer in two groups: new uploads of the subscription
channels (config/channels.yaml, published in the last morning_offer.subscription_days days and
not offered on an earlier day), then the backlog of the NarrativeRadar queue and the playback
queue (caption-blocked videos), under the queue's rules (keyword-search rows excluded, 30-day age
cap). The user answers with numbers and processing starts at once. For each chosen video the bot
reads the official visible transcript and writes the daily summary; a subscription video gets
only the daily summary (user decision 2026-11-19), a NarrativeRadar video both outputs. If there
is no visible transcript, the video is marked as a playback request, and the app's scheduled
task (youtube-supervised-playback, daily at PLAYBACK_RUN_TIME) plays it in the desktop app's
browser pane through realtime_worker.py record, delivers the summary and sends it with
``playback_bot.py digest``. Automated Chrome playback runs only if
transcript_fallback.automated_playback is on (off: YouTube stops it at ~40 s).

The briefing goes out once a day when the chosen videos are done, at once when the user answers
'없음' or the offer is empty, and at BRIEF_DEADLINE_HOUR with whatever is done by then. Videos
finished after it follow in one more message when the session ends. '브리핑' sends it again.
The market and dashboard parts come from ~/TrillionStack/scripts/morning_brief.py, the video
part from ``main.py --brief-digest`` in the Airflow container.
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
from datetime import date, datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))

from autoplay import AutoPlayer, CaptureBusy, PlaybackStopped
from channel_priority import prioritize_channels
from realtime_fallback import PlaybackQueue, get_project_root, load_config
from state_manager import StateManager

KST = ZoneInfo('Asia/Seoul')
OFFER_HOUR = 8
# Processing starts as soon as the user picks after the offer (user decision 2026-11-19).
WINDOW_START, WINDOW_END = OFFER_HOUR, 23
BRIEF_DEADLINE_HOUR = 10
BUDGET_SECONDS = 3 * 3600
OFFER_SIZE = 30
MAX_AGE_DAYS = 30
OFFERED_KEEP_DAYS = 30
PLAYBACK_RUN_TIME = '13:00'
STOP_POLL_SECONDS = 15
TELEGRAM_CHUNK = 3500
CONTAINER = 'devenv-airflow-scheduler'
ENV_FILE = Path.home() / 'PersonalAgent' / '.env'
MORNING_BRIEF = Path.home() / 'TrillionStack' / 'scripts' / 'morning_brief.py'
WEEKDAYS = '월화수목금토일'
CONTINUE_WORDS = {'계속', '예', '네', '응', '진행', 'yes', 'y'}
STOP_WORDS = {'중단', '그만', '정지', '아니오', '아니요', 'stop', 'no', 'n'}
STATUS_WORDS = {'상태', 'status'}
LIST_WORDS = {'목록', 'list'}
BRIEF_WORDS = {'브리핑', 'brief'}
HELP = ("번호로 답해 주세요. 예: 1,3,5-8 · '전체' · '없음'\n"
        "그 밖에 '상태', '목록', '중단', '브리핑'을 쓸 수 있습니다.")

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


def chunks(text, size=TELEGRAM_CHUNK):
    """Pieces of at most `size` characters, cut at line ends where a line allows it."""
    pieces, current = [], ''
    for line in text.split('\n'):
        while len(line) > size:
            if current:
                pieces.append(current)
                current = ''
            pieces.append(line[:size])
            line = line[size:]
        candidate = f'{current}\n{line}' if current else line
        if len(candidate) > size:
            pieces.append(current)
            current = line
        else:
            current = candidate
    if current.strip():
        pieces.append(current)
    return pieces


class Telegram:
    def __init__(self, token, chat_id, user_id, timeout=40):
        self._base = f'https://api.telegram.org/bot{token}'
        self.chat_id, self.user_id, self.timeout = str(chat_id), str(user_id), timeout

    def send(self, text):
        for piece in chunks(text):
            self._call('sendMessage', {'chat_id': self.chat_id, 'text': piece,
                                       'disable_web_page_preview': 'true'})

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


def day_label(day):
    day = date.fromisoformat(day) if isinstance(day, str) else day
    return f'{day:%m-%d}({WEEKDAYS[day.weekday()]})'


def is_subscription(item):
    return item.get('source') == 'subscription'


def format_offer(day, items, in_window, carried, note=None):
    subscribed = sum(is_subscription(item) for item in items)
    lines = [f'YouTube 요약 후보 · {day_label(day)}',
             f'구독 채널 새 영상 {subscribed}건, 수집 대기 {len(items) - subscribed}건'
             f' (기한 내 미처리 {in_window}건 중)'
             + (f', 이전 목록에서 이월 {len(carried)}건' if carried else '')]
    if note:
        lines.append(note)
    group = None
    for number, item in enumerate(items, start=1):
        current = 'subscription' if is_subscription(item) else 'backlog'
        if current != group:
            group = current
            lines += ['', '[구독 채널 새 영상]' if current == 'subscription'
                      else '[수집 대기 영상 · 내러티브·재생 대기]']
        title = item['title'] if len(item['title']) <= 70 else item['title'][:69] + '…'
        lines.append(f"{number}. [{item['channel_title']}] {title} · {minutes(item['duration_seconds'])}분")
    if not items:
        lines += ['', '오늘은 새 후보가 없습니다. 아침 브리핑만 보냅니다.']
        return '\n'.join(lines)
    total = sum(item['duration_seconds'] for item in items)
    lines += ['', f'전체 재생 시간 약 {hours_text(total)}', HELP,
              '고른 영상은 바로 화면 자막으로 요약하고, 다 끝나면 아침 브리핑(증시·영상 정리·대시보드)을 '
              f'보냅니다. {BRIEF_DEADLINE_HOUR}:00까지 끝나지 않으면 그때까지 된 것만 담아 보냅니다. '
              f'화면 자막이 없는 영상은 {PLAYBACK_RUN_TIME} 예약 작업이 재생·요약해 따로 보냅니다.']
    return '\n'.join(lines)


def format_videos(digest, title):
    """Synthesis first, then each video in a few lines."""
    lines = [f"{title} {len(digest['videos'])}건"]
    if digest.get('synthesis'):
        lines += ['', '종합', digest['synthesis']]
    for number, video in enumerate(digest['videos'], start=1):
        lines += ['', f"{number}. [{video.get('channel') or ''}] {video.get('title') or video['video_id']}"]
        if video.get('overview'):
            lines.append(video['overview'])
        lines += [f'- {point}' for point in video.get('points') or []]
        lines.append(video.get('url') or f"https://youtu.be/{video['video_id']}")
    if digest.get('missing'):
        lines += ['', f"요약 파일을 찾지 못한 영상 {len(digest['missing'])}건: {', '.join(digest['missing'])}"]
    return '\n'.join(lines)


def too_old(item, now):
    """Same rule as queue_consumer._too_old: naive timestamps are UTC."""
    try:
        published = datetime.fromisoformat((item.get('published_at') or '').replace('Z', '+00:00'))
    except ValueError:
        return False
    if published.tzinfo is None:
        published = published.replace(tzinfo=ZoneInfo('UTC'))
    return published < now - timedelta(days=MAX_AGE_DAYS)


def excluded(item):
    """Discovery modes the queue does not process (config.yaml narrative_queue)."""
    modes = (load_config().get('narrative_queue') or {}).get('excluded_discovery_modes') or []
    return (item.get('discovery') or {}).get('mode') in modes


class PlaybackBot:
    def __init__(self, telegram, store, *, fetch_candidates, play, deliver, is_processed,
                 brief=None, digest=None, clock=time.time):
        self.telegram, self.store = telegram, store
        self.fetch_candidates, self.play, self.deliver = fetch_candidates, play, deliver
        self.is_processed, self.clock = is_processed, clock
        # brief() -> {'market', 'dashboard', 'errors'}; digest(ids) -> {'videos', 'missing', 'synthesis'}
        self.brief, self.digest = brief, digest

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
        self._maybe_brief(self.now())

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

    # -- the day's record ---------------------------------------------------

    def _day(self, now=None):
        """Today's videos and briefing state; a new day starts empty."""
        today = (now or self.now()).date().isoformat()
        day = self.store.get('day')
        if not day or day.get('date') != today:
            day = {'date': today, 'done': [], 'requested': [], 'failed': [], 'briefed': [],
                   'brief_due': False, 'brief_sent': False}
            self.store.set('day', day)
        return day

    def _note(self, key, video_id):
        day = self._day()
        if video_id not in day[key]:
            day[key].append(video_id)
            self.store.set('day', day)

    def _brief_due(self):
        day = self._day()
        day['brief_due'] = True
        self.store.set('day', day)

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
        carried = [item for item in carried if not self.is_processed(item)
                   and not too_old(item, now) and not excluded(item)]
        carried = list({item['video_id']: item for item in carried}.values())
        seen = {item['video_id'] for item in carried}
        # A subscription upload is offered on one day only: one not picked is not offered again.
        offered = {vid: when for vid, when in (self.store.get('offered') or {}).items()
                   if (now.date() - date.fromisoformat(when)).days <= OFFERED_KEEP_DAYS}
        fresh = [item for item in offer['items'] if item['video_id'] not in seen
                 and not (is_subscription(item) and offered.get(item['video_id'], day) != day)]
        config = load_config()
        pool = carried + fresh
        items = (prioritize_channels([i for i in pool if is_subscription(i)], lambda item: item.get('channel_id'), config)
                 + prioritize_channels([i for i in pool if not is_subscription(i)],
                                       lambda item: item.get('channel_id'), config))[:OFFER_SIZE]
        offered.update({item['video_id']: day for item in items if is_subscription(item)})
        note = ('구독 채널 목록을 읽지 못해 수집 대기 영상만 보냅니다: ' + offer['subscription_error']
                if offer.get('subscription_error') else None)
        self._day(now)
        self._say(format_offer(day, items, offer['in_window'], carried, note))
        logger.info('offer sent for %s: %s items, %s carried', day, len(items), len(carried))
        self.store.set('offer', {'date': day, 'items': items, 'in_window': offer['in_window']})
        self.store.set('offer_date', day)
        self.store.set('offered', offered)
        self.store.set('carry', [])
        if not items:
            self._brief_due()

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
        if text.lower() in BRIEF_WORDS:
            self._send_brief(again=True)
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
            self._say('오늘은 영상을 처리하지 않습니다. 아침 브리핑만 보냅니다.')
            self._brief_due()
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
        start = '' if self._in_window(self.now()) else f' {WINDOW_START:02d}:00부터 시작합니다.'
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
                      f"재생 요청 {len(session.get('requested', []))}건, 재생·전사 대기 {len(session.get('deferred', []))}건, "
                      f"남은 {len(session['queue'])}건(재생 약 {hours_text(left)}).\n"
                      "3시간 더 진행할까요? '계속' 또는 '중단'으로 답해 주세요.")
            return
        ordered = prioritize_channels(session['queue'], lambda item: item.get('channel_id'), load_config())
        if ordered != session['queue']:
            session['queue'] = ordered
            self.store.set('session', session)
        item = ordered[0]
        now = self.now()
        if self.is_processed(item) or too_old(item, now):
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
                if outcome.get('busy'):
                    outcome['status'] = 'busy'
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
                reason = '해당 영상의 요약이 진행 중' if outcome.get('busy') else '다른 녹음(강의 등)이 진행 중'
                self._say(f'{reason}이라 10분 뒤 다시 시도합니다.')
            return
        if outcome['status'] == 'stopped':
            self._stop(session, '현재 영상을 멈추고 중단했습니다. 이 영상은 내일 목록에 다시 올립니다.')
            return
        session['queue'].pop(0)
        session['busy_told'] = False
        if outcome['status'] == 'ready':
            session['done'].append(item['video_id'])
            self._note('done', item['video_id'])
            self._say(f"완료 {len(session['done'])}건: [{item['channel_title']}] {item['title'][:60]}")
        elif outcome['status'] == 'requested':
            session.setdefault('requested', []).append(item['video_id'])
            self._note('requested', item['video_id'])
            self._say(f"재생 요청: [{item['channel_title']}] {item['title'][:60]} "
                      f"(화면 자막 없음, {PLAYBACK_RUN_TIME} 예약 작업이 재생합니다)")
        elif outcome['status'] == 'deferred':
            session.setdefault('deferred', []).append(item['video_id'])
            self.store.set('carry', self.store.get('carry', []) + [item])
            self._say(f"재생·전사 대기: [{item['channel_title']}] {item['title'][:60]} "
                      '(일반 브라우저에서 재생 후 요약을 재개합니다)')
        else:
            session['failed'].append(item['video_id'])
            self._note('failed', item['video_id'])
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
                  f"재생 요청 {len(session.get('requested', []))}건, 재생·전사 대기 {len(session.get('deferred', []))}건, "
                  f"처리 시간 {hours_text(session['elapsed'])}. 남은 {len(session['queue'])}건은 "
                  '내일 08:00 목록에 다시 올립니다.')
        self._session_ended()

    def _finish(self, session):
        self.store.set('session', None)
        self._say(f"선택한 영상 처리를 마쳤습니다. 완료 {len(session['done'])}건, "
                  f"실패 {len(session['failed'])}건, 재생 요청 {len(session.get('requested', []))}건, 재생·전사 대기 {len(session.get('deferred', []))}건, "
                  f"처리 시간 {hours_text(session['elapsed'])}.")
        self._session_ended()

    def _session_ended(self):
        """Brief now, or follow a brief already sent with the videos finished after it."""
        day = self._day()
        if not day['brief_sent']:
            self._brief_due()
            return
        later = [vid for vid in day['done'] if vid not in day['briefed']]
        if later:
            self._say(self._video_part(later, '[추가 영상 정리]'))
            day = self._day()
            day['briefed'] += later
            self.store.set('day', day)

    def _status(self, session):
        if not session:
            return '진행 중인 작업이 없습니다.'
        state = '계속 여부 답변 대기' if session.get('awaiting') else '처리 중'
        return (f"{state}: 완료 {len(session['done'])}건, 실패 {len(session['failed'])}건, "
                f"재생 요청 {len(session.get('requested', []))}건, 재생·전사 대기 {len(session.get('deferred', []))}건, "
                f"남은 {len(session['queue'])}건, 처리 시간 {hours_text(session['elapsed'])} "
                f"/ 확인 시점 {hours_text(session['budget'])}")

    # -- morning briefing ---------------------------------------------------

    def _maybe_brief(self, now):
        if self.brief is None or now.hour < OFFER_HOUR:
            return
        today = now.date().isoformat()
        day = self.store.get('day')
        if day and day.get('date') == today and day.get('brief_sent'):
            return
        offered = self.store.get('offer_date') == today
        if (offered and day and day.get('date') == today and day.get('brief_due')) \
                or now.hour >= BRIEF_DEADLINE_HOUR:
            self._send_brief()

    def _video_part(self, video_ids, title):
        if not video_ids:
            return f'{title}\n오늘 요약한 구독 영상이 없습니다.'
        try:
            return format_videos(self.digest(video_ids), title)
        except Exception as error:
            logger.exception('video digest failed')
            return (f'{title}\n영상 정리를 만들지 못했습니다({str(error)[:160]}). '
                    f'요약 {len(video_ids)}건은 Youtube_summary/summaries 폴더에 있습니다.')

    def _send_brief(self, again=False):
        """The three briefing messages: market, videos, dashboard."""
        if self.brief is None:
            self._say('브리핑 구성 요소가 연결되지 않았습니다.')
            return
        now = self.now()
        day = self._day(now)
        videos = list(day['done']) if again else [vid for vid in day['done'] if vid not in day['briefed']]
        try:
            data = self.brief()
            market, dashboard = data['market'], data['dashboard']
            if data.get('errors'):
                logger.warning('brief errors: %s', data['errors'])
        except Exception as error:
            logger.exception('morning brief failed')
            market = dashboard = f'자료를 만들지 못했습니다({str(error)[:160]}).'
        self._say(f'아침 브리핑 · {day_label(now.date())}\n\n[증시]\n{market}')
        notes = []
        session = self.store.get('session')
        if session and session['queue']:
            notes.append(f"처리 중인 {len(session['queue'])}건은 끝나면 추가로 보냅니다.")
        waiting = [vid for vid in day['requested'] if vid not in day['done']]
        if waiting:
            notes.append(f'화면 자막이 없는 {len(waiting)}건은 {PLAYBACK_RUN_TIME} 재생 작업이 요약해 따로 보냅니다.')
        if day['failed']:
            notes.append(f"요약하지 못한 영상 {len(day['failed'])}건.")
        self._say(self._video_part(videos, '[구독 영상 정리]') + ('\n\n' + '\n'.join(notes) if notes else ''))
        self._say(f'[기억해 둘 것]\n\n{dashboard}')
        day = self._day(now)
        day['briefed'] = sorted(set(day['briefed']) | set(videos))
        day['brief_sent'] = True
        self.store.set('day', day)
        logger.info('brief sent for %s with %s videos', day['date'], len(videos))


# -- real collaborators ------------------------------------------------------

def _container(*args, timeout):
    result = subprocess.run(['docker', 'exec', '-w', '/opt/airflow/src', CONTAINER, 'python', 'main.py', *args],
                            capture_output=True, text=True, timeout=timeout)
    lines = [line for line in result.stdout.splitlines() if line.startswith('{')]
    if not lines:
        raise RuntimeError(f'main.py {args[0]} exit {result.returncode}: {result.stderr[-300:]}')
    return json.loads(lines[-1])


def fetch_candidates():
    # A margin beyond the offer: subscription uploads offered on an earlier day drop out here.
    return _container('--candidates', str(OFFER_SIZE * 2), timeout=300)


def fetch_brief():
    result = subprocess.run([sys.executable, str(MORNING_BRIEF), '--json'], capture_output=True, text=True,
                            timeout=240, cwd=str(MORNING_BRIEF.parents[1]))
    lines = [line for line in result.stdout.splitlines() if line.startswith('{')]
    if not lines:
        raise RuntimeError(f'morning_brief exit {result.returncode}: {result.stderr[-300:]}')
    return json.loads(lines[-1])


def fetch_digest(video_ids):
    return _container('--brief-digest', ','.join(video_ids), timeout=600)


def deliver(video_id):
    # Zero written without errors means NAR-08 delivered the same transcript first.
    result = _container('--deliver-ready', video_id, timeout=900)
    if result['errors']:
        raise RuntimeError(f'summaries not written: {result}')
    return result


def destinations(item, queue=None):
    """Outputs a picked video gets: subscription → daily; waiting job → its own; NarrativeRadar → both."""
    if is_subscription(item):
        return ('daily',)
    if item.get('source') == 'waiting':
        own = (queue or PlaybackQueue()).destinations(item['video_id'])
        if own:
            return tuple(own)
    return ('narrative', 'daily')


def is_processed(item):
    state = StateManager()
    queue = PlaybackQueue()
    video_id = item['video_id']
    return all(state.is_video_processed(video_id, destination) or queue.delivery_output(video_id, destination)
               for destination in destinations(item, queue))


def play(item, stop_requested):
    queue = PlaybackQueue()
    state = StateManager()
    payload = {key: item.get(key) for key in ('video_id', 'title', 'channel_title', 'channel_id',
                                              'channel_handle', 'published_at', 'url', 'discovery')}
    # A waiting playback job keeps its own destinations.
    if item.get('source') != 'waiting':
        for destination in destinations(item, queue):
            if not state.is_video_processed(item['video_id'], destination):
                queue.enqueue(payload, 'user_selected', destination)
    job = queue.get(item['video_id'])
    if job is None or job['status'] == 'ready':
        return {'status': 'ready', 'chars': 0}
    automated = (load_config().get('transcript_fallback') or {}).get('automated_playback') is True
    if job['next_action'] == 'playback' and not automated:
        outcome = {'status': 'deferred', 'next_action': 'playback'}
    else:
        try:
            with AutoPlayer(queue) as player:
                outcome = player.play(item, stop_requested)
        except CaptureBusy as error:
            # A course recording holds the lock: retry later instead of ending the session.
            return {'status': 'busy', 'error': str(error)}
    if outcome['status'] == 'deferred' and outcome.get('next_action') == 'playback':
        queue.request_playback(item['video_id'], item)
        return {'status': 'requested'}
    return outcome


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('command', choices=('serve', 'offer', 'notify', 'brief', 'digest'),
                        help="serve: run forever; offer: send today's offer now; notify: send TEXT; "
                             "brief: send today's briefing now; digest: send the summaries of IDS")
    parser.add_argument('text', nargs='?', help='notify: message for the conversation chat; digest: comma-separated IDs')
    args = parser.parse_args()
    if args.command == 'notify':
        # Used by the supervised playback task to report its run in the same chat.
        if not args.text:
            parser.error('notify needs TEXT')
        Telegram(*load_settings()).send(args.text)
        return
    if args.command == 'digest':
        # Used by the supervised playback task: the afternoon summaries of the played videos.
        ids = [vid for vid in (args.text or '').split(',') if vid]
        if not ids:
            parser.error('digest needs IDS')
        Telegram(*load_settings()).send(format_videos(fetch_digest(ids), '[오후 영상 정리]'))
        return
    log_dir = get_project_root() / 'logs'
    log_dir.mkdir(exist_ok=True)
    handler = RotatingFileHandler(log_dir / 'playback_bot.log', maxBytes=5_000_000, backupCount=3)
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    logging.basicConfig(level=logging.INFO, handlers=[handler, logging.StreamHandler()])
    bot = PlaybackBot(Telegram(*load_settings()), Store(get_project_root() / 'data' / 'playback_bot.sqlite3'),
                      fetch_candidates=fetch_candidates, play=play, deliver=deliver, is_processed=is_processed,
                      brief=fetch_brief, digest=fetch_digest)
    if args.command == 'offer':
        bot.store.set('offer_date', None)
        bot.store.set('offer_retry_at', 0)
        bot._maybe_offer(bot.now().replace(hour=max(OFFER_HOUR, bot.now().hour)))
    elif args.command == 'brief':
        bot._send_brief(again=True)
    else:
        bot.serve()


if __name__ == '__main__':
    main()

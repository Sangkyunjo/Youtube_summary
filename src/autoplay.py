"""Host transcript recovery: official visible UI first, optional automated playback.

The official watch page plays at 1x in a dedicated Chrome profile while
realtime_worker.py records system audio. Player state is read from the page every
quarter second and submitted as worker evidence, so ads and stalls can be cut
from the recording afterwards. A bot check or consent wall stops the session:
it is reported, never worked around.
"""
import logging
import math
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from realtime_fallback import get_project_root, interrupted, load_config
from browser_transcript import VisibleTranscriptReader

logger = logging.getLogger('playback_bot')

# Multi-output device (Mac mini speakers + BlackHole) and the loopback it feeds.
CAPTURE_OUTPUT = 'Fanding Capture'
CAPTURE_INPUT = 'BlackHole 2ch'
OUTPUT_TOOL_SOURCE = Path.home() / 'PersonalAgent' / 'fanding-kb' / 'lib' / 'fkb_set_output.swift'

POLL_SECONDS = 0.25
SUBMIT_EVERY = 5
READY_TIMEOUT = 90
TRANSCRIBE_TIMEOUT = 3600

SNAPSHOT_JS = """() => {
  const v = document.querySelector('video');
  const p = document.querySelector('#movie_player');
  if (!v || !p) return null;
  const ad = p.classList.contains('ad-showing');
  return {video_id: new URL(location.href).searchParams.get('v'),
          current_time: v.currentTime, duration: v.duration, rate: v.playbackRate,
          paused: v.paused, ended: v.ended && !ad, muted: v.muted || v.volume === 0, ad,
          buffering: !v.ended && (v.readyState < 3 || !isFinite(v.duration)),
          captured_at: Date.now() / 1000};
}"""

# Distinguish an explicit bot/consent wall from a generic player error.
WALL_JS = """() => {
  const text = document.body ? document.body.innerText.slice(0, 5000) : '';
  if (location.hostname.startsWith('consent.') || document.querySelector('iframe[src*="recaptcha"]')
      || /not a bot|unusual traffic|로봇이 아닙니다|봇이 아님|비정상적인 트래픽/i.test(text)) return 'bot_check';
  const error = document.querySelector('.ytp-error');
  return error && error.offsetParent !== null ? 'player_error' : null;
}"""

WALL_MESSAGES = {
    'bot_check': 'YouTube가 봇 확인이나 동의 화면을 띄웠습니다',
    'player_error': 'YouTube 플레이어가 오류 화면으로 멈췄습니다. 원인은 추가 확인이 필요합니다',
}


def refused(reason):
    return PlaybackStopped(f'{WALL_MESSAGES[reason]}. 우회하지 않습니다')

AUTOPLAY_OFF_JS = """() => {
  const toggle = document.querySelector('.ytp-autonav-toggle-button');
  if (toggle && toggle.getAttribute('aria-checked') === 'true') (toggle.closest('button') || toggle).click();
  return toggle ? toggle.getAttribute('aria-checked') : null;
}"""

SKIP_AD_JS = """() => {
  const button = document.querySelector('.ytp-skip-ad-button, .ytp-ad-skip-button-modern, .ytp-ad-skip-button');
  if (button && button.offsetParent !== null) { button.click(); return true; }
  return false;
}"""

READY_AT_ZERO_JS = """() => {
  const v = document.querySelector('video');
  v.pause(); v.currentTime = 0; v.playbackRate = 1; v.muted = false;
  if (v.volume === 0) v.volume = 1;
}"""


class PlaybackStopped(Exception):
    """The whole session must stop (bot check, consent wall, browser gone, no recorder)."""


def default_output():
    """Current macOS default output device name, read the same way fkb-rec reads it."""
    text = subprocess.run(['system_profiler', 'SPAudioDataType'], capture_output=True,
                          text=True, timeout=60).stdout
    name = None
    for line in text.splitlines():
        if re.match(r'^\s+\S.*:$', line):
            name = line.strip()[:-1]
        elif 'Default Output Device: Yes' in line:
            return name
    return None


def set_output(name):
    """Select a default output with fanding-kb's CoreAudio tool, built on first use."""
    tool = get_project_root() / 'data' / '.fkb-set-output'
    if not tool.exists() or OUTPUT_TOOL_SOURCE.stat().st_mtime > tool.stat().st_mtime:
        subprocess.run(['swiftc', str(OUTPUT_TOOL_SOURCE), '-o', str(tool)],
                       check=True, capture_output=True, timeout=300)
    result = subprocess.run([str(tool), name], capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise PlaybackStopped(f'소리 출력을 {name}(으)로 바꾸지 못했습니다: {result.stderr.strip()}')


class AudioRoute:
    """Send system output to the capture device for one recording, then put it back."""

    def __enter__(self):
        self.previous = default_output()
        if self.previous != CAPTURE_OUTPUT:
            set_output(CAPTURE_OUTPUT)
        return self

    def __exit__(self, *_exc):
        if self.previous and self.previous != CAPTURE_OUTPUT:
            try:
                set_output(self.previous)
            except Exception:
                logger.exception('could not restore audio output to %s', self.previous)


class AutoPlayer:
    def __init__(self, queue, profile_dir=None, worker_python=None):
        self.queue = queue
        self.profile_dir = Path(profile_dir or get_project_root() / 'data' / 'chrome-profile')
        self.worker_python = worker_python or sys.executable
        self._playwright = None
        self._context = None
        self._route = None
        fallback = load_config().get('transcript_fallback') or {}
        self.ui_enabled = fallback.get('ui_enabled', True) is True
        self.automated_playback = fallback.get('automated_playback', False) is True
        self.ui_reader = VisibleTranscriptReader(fallback.get('ui_timeout_seconds', 20))

    def __enter__(self):
        from playwright.sync_api import sync_playwright

        # Chrome keeps the output device it opened first, so switch before it starts.
        if self.automated_playback:
            self._route = AudioRoute().__enter__()
        try:
            self.profile_dir.mkdir(parents=True, exist_ok=True)
            self._playwright = sync_playwright().start()
            # Playwright mutes Chrome by default; the recorder needs real system audio.
            self._context = self._playwright.chromium.launch_persistent_context(
                str(self.profile_dir), channel='chrome', headless=False,
                ignore_default_args=['--mute-audio'] if self.automated_playback else [],
                args=['--autoplay-policy=no-user-gesture-required'] if self.automated_playback
                else ['--autoplay-policy=document-user-activation-required'])
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *_exc):
        try:
            if self._context:
                self._context.close()
        finally:
            try:
                if self._playwright:
                    self._playwright.stop()
            finally:
                if self._route:
                    self._route.__exit__(None, None, None)

    def play(self, item, tick=lambda: False):
        """Play one video to its end while the worker records. Returns an outcome dict."""
        vid = item['video_id']
        page = self._context.pages[0] if self._context.pages else self._context.new_page()
        try:
            page.goto(f'https://www.youtube.com/watch?v={vid}', wait_until='domcontentloaded', timeout=60000)
            job = self.queue.get(vid)
            if job and job['status'] == 'ready':
                return {'status': 'ready', 'chars': len(job['transcript'] or '')}
            if self.ui_enabled and job and job['next_action'] == 'ui_transcript':
                # Pause visible media while reading text; no full playback or recorder needed.
                visible_video = page.locator('video')
                if visible_video.count():
                    visible_video.first.evaluate('(video) => video.pause()')
                if page.evaluate(WALL_JS) == 'bot_check':
                    raise refused('bot_check')
                try:
                    document, status = self.ui_reader.read(page, item, tick)
                except Exception as error:
                    document, status = None, f'ui_error:{type(error).__name__}'
                if status == 'stopped':
                    return {'status': 'stopped'}
                if document:
                    imported = self.queue.import_ui(vid, document)
                    return {'status': 'ready', 'chars': len(imported['transcript']),
                            'acquisition': 'youtube_visible_transcript'}
                self.queue.ui_unavailable(vid, status)
            elif job and job['next_action'] == 'ui_transcript' and not self.ui_enabled:
                self.queue.ui_unavailable(vid, 'ui_disabled')
            if not self.automated_playback:
                return {'status': 'deferred', 'next_action': 'playback',
                        'error': '공식 스크립트를 확보하지 못했습니다. 일반 브라우저 재생·전사가 필요합니다'}
            self._wait_for_player(page, vid)
            # The switch value updates after the click; the content end stops the
            # recorder either way, so a stuck switch is not fatal.
            for _ in range(10):
                if page.evaluate(AUTOPLAY_OFF_JS) != 'true':
                    break
                time.sleep(0.5)
            state = self._finish_pre_roll(page, vid)
        except PlaybackStopped:
            raise
        except Exception as error:
            return {'status': 'failed', 'error': f'player: {error}'}
        duration = state['duration']
        if not (isinstance(duration, (int, float)) and math.isfinite(duration) and 1 <= duration <= 21600):
            return {'status': 'failed', 'error': 'not a finite, non-live video'}
        page.evaluate(READY_AT_ZERO_JS)
        logger.info('%s: player ready at 0, %.0fs of content', vid, duration)
        seconds = min(21615, math.ceil(duration * 1.25) + 600)
        worker = Recorder(self.worker_python, vid, seconds)
        try:
            if not worker.wait_ready(READY_TIMEOUT):
                worker.stop()
                if worker.busy():
                    return {'status': 'busy', 'error': 'another capture is active'}
                # Every later video would fail the same way, so stop the session.
                raise PlaybackStopped('녹음을 시작하지 못했습니다: ' + worker.tail())
            logger.info('%s: recording, playback started', vid)
            page.evaluate("() => document.querySelector('video').play()")
            outcome = self._observe(page, vid, duration, worker, tick)
            if outcome:
                worker.stop()
                page.evaluate("() => document.querySelector('video').pause()")
                return outcome
        except PlaybackStopped:
            worker.stop()
            raise
        # Recording is over; the worker trims and transcribes.
        logger.info('%s: content ended, transcribing', vid)
        if not worker.wait_exit(TRANSCRIBE_TIMEOUT):
            worker.stop()
            return {'status': 'failed', 'error': 'transcription timed out'}
        job = self.queue.get(vid)
        if worker.returncode == 0 and job and job['status'] == 'ready':
            return {'status': 'ready', 'chars': len(job['transcript'] or '')}
        return {'status': 'failed', 'error': (job or {}).get('error') or worker.tail()}

    def _wait_for_player(self, page, vid):
        deadline = time.time() + 45
        while time.time() < deadline:
            reason = page.evaluate(WALL_JS)
            if reason:
                raise refused(reason)
            state = page.evaluate(SNAPSHOT_JS)
            if state and state['video_id'] == vid:
                return state
            time.sleep(1)
        raise RuntimeError('player did not load')

    def _finish_pre_roll(self, page, vid):
        deadline = time.time() + 300
        while time.time() < deadline:
            state = page.evaluate(SNAPSHOT_JS)
            if state and state['video_id'] == vid and not state['ad'] and math.isfinite(state['duration'] or float('nan')):
                return state
            page.evaluate(SKIP_AD_JS)
            time.sleep(1)
        raise RuntimeError('pre-roll ad did not finish')

    def _observe(self, page, vid, duration, worker, tick):
        """Submit evidence until the content ends. Returns an outcome only on failure."""
        deadline = time.time() + duration * 1.5 + 900
        last_content, last_sent, was_interrupted, paused_since = None, 0.0, False, None

        def submit(sample):
            nonlocal last_sent
            self.queue.observe(vid, sample)
            last_sent = sample['captured_at']

        while True:
            if tick():
                return {'status': 'stopped', 'error': 'stopped by user'}
            if worker.returncode is not None:
                return {'status': 'failed', 'error': 'recorder exited: ' + worker.tail()}
            if time.time() > deadline:
                return {'status': 'failed', 'error': 'playback took too long'}
            try:
                reason = page.evaluate(WALL_JS)
                if reason:
                    raise refused(reason)
                sample = page.evaluate(SNAPSHOT_JS)
            except PlaybackStopped:
                raise
            except Exception as error:
                raise PlaybackStopped(f'Chrome 페이지가 닫혔습니다 ({error})') from None
            if not sample or sample['video_id'] != vid:
                return {'status': 'failed', 'error': 'page left the video'}
            now_interrupted = interrupted(sample)
            if not now_interrupted:
                if was_interrupted or sample['ended'] or sample['captured_at'] - last_sent >= SUBMIT_EVERY:
                    submit(sample)
                last_content, paused_since = sample, None
                if sample['ended']:
                    return None
            else:
                if not was_interrupted and last_content and last_content['captured_at'] > last_sent:
                    submit(last_content)
                if not was_interrupted or sample['captured_at'] - last_sent >= SUBMIT_EVERY:
                    submit(sample)
                if sample['ad']:
                    page.evaluate(SKIP_AD_JS)
                elif sample['paused'] and not sample['buffering']:
                    # An unexpected pause stops content like an ad; resume and cut it out.
                    paused_since = paused_since or time.time()
                    if time.time() - paused_since > 1:
                        page.evaluate("() => document.querySelector('video').play()")
            was_interrupted = now_interrupted
            time.sleep(POLL_SECONDS)


class Recorder:
    """realtime_worker.py record in cut mode, with its output captured for status."""

    def __init__(self, python, vid, seconds):
        worker = Path(__file__).resolve().parent / 'realtime_worker.py'
        env = dict(os.environ)
        # fkb-rec runs `python3` for its lock and config; use this interpreter's environment.
        env['PATH'] = f'{Path(python).parent}{os.pathsep}{env.get("PATH", "")}'
        self._process = subprocess.Popen(
            [python, str(worker), 'record', vid, '--seconds', str(seconds), '--cut-interruptions',
             '--capture-device', CAPTURE_INPUT, '--output-device', CAPTURE_OUTPUT],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
        self._lines = []
        self._ready = threading.Event()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self._process.stdout:
            self._lines.append(line.rstrip())
            del self._lines[:-200]
            if 'CAPTURE_READY' in line:
                self._ready.set()
        self._ready.set()

    @property
    def returncode(self):
        return self._process.poll()

    def wait_ready(self, timeout):
        return self._ready.wait(timeout) and self._process.poll() is None

    def wait_exit(self, timeout):
        try:
            self._process.wait(timeout)
            return True
        except subprocess.TimeoutExpired:
            return False

    def busy(self):
        return any('capture is active' in line for line in self._lines)

    def stop(self):
        if self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(30)
            except subprocess.TimeoutExpired:
                self._process.kill()

    def tail(self):
        # fkb-rec colours its messages; keep the plain text.
        plain = [re.sub(r'\x1b\[[0-9;]*m', '', line).strip() for line in self._lines[-4:]]
        return ' | '.join(line[-160:] for line in plain if line)

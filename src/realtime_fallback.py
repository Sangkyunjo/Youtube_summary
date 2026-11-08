"""Durable handoff between container discovery and supervised Mac playback."""
import json
import math
import re
import sqlite3
import statistics
import time
from contextlib import contextmanager
from pathlib import Path

import yaml

from transcript_acquisition import (API_SOURCE, LOCAL_SOURCE, acquisition,
                                    validate_ui_document)

# jobs.model value for a transcript that came from captions, not from playback.
CAPTION_MODEL = 'youtube-captions'


def get_project_root():
    return Path(__file__).resolve().parent.parent


def load_config():
    return yaml.safe_load((get_project_root() / 'config/config.yaml').read_text())


def enabled():
    return bool(load_config().get('realtime_fallback', {}).get('enabled', False))


def video_id(value):
    if not re.fullmatch(r'[A-Za-z0-9_-]{11}', value):
        raise ValueError('Expected an 11-character YouTube video ID')
    return value


class PlaybackQueue:
    def __init__(self, path=None):
        self.path = Path(path or get_project_root() / 'data' / 'realtime.sqlite3')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('''CREATE TABLE IF NOT EXISTS jobs (
                video_id TEXT PRIMARY KEY, payload TEXT NOT NULL, reason TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', transcript TEXT,
                started REAL, updated REAL NOT NULL, error TEXT,
                evidence TEXT NOT NULL DEFAULT '[]', model TEXT)''')
            db.execute('''CREATE TABLE IF NOT EXISTS deliveries (
                video_id TEXT, destination TEXT, output TEXT,
                PRIMARY KEY(video_id, destination))''')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(jobs)')}
            if 'acquisition' not in columns:
                db.execute("ALTER TABLE jobs ADD COLUMN acquisition TEXT NOT NULL DEFAULT '{}'")
            if 'next_action' not in columns:
                db.execute("ALTER TABLE jobs ADD COLUMN next_action TEXT NOT NULL DEFAULT 'ui_transcript'")
                db.execute("UPDATE jobs SET next_action='deliver' WHERE status='ready'")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def enqueue(self, payload, reason, destination='daily'):
        vid = video_id(payload['video_id'])
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO jobs(video_id,payload,reason,updated) VALUES(?,?,?,?)',
                       (vid, json.dumps(payload, ensure_ascii=False), reason, time.time()))
            db.execute('INSERT OR IGNORE INTO deliveries(video_id,destination) VALUES(?,?)',
                       (vid, destination))
        return self.get(vid)

    def get(self, vid):
        with self.connect() as db:
            row = db.execute('SELECT * FROM jobs WHERE video_id=?', (video_id(vid),)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result['payload'] = json.loads(result['payload'])
        result['evidence'] = json.loads(result['evidence'])
        result['acquisition'] = json.loads(result['acquisition'])
        return result

    def list(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute(
                '''SELECT j.video_id,j.status,j.reason,j.error,j.updated,j.next_action,
                    (SELECT count(*) FROM deliveries d WHERE d.video_id=j.video_id
                     AND d.output IS NULL) AS pending_outputs
                   FROM jobs j ORDER BY j.updated,j.video_id''' )]

    def ready(self, destination):
        with self.connect() as db:
            rows = db.execute('''SELECT j.video_id FROM jobs j JOIN deliveries d
                ON j.video_id=d.video_id WHERE j.status='ready' AND
                d.destination=? AND d.output IS NULL ORDER BY j.updated''', (destination,)).fetchall()
        return [self.get(r['video_id']) for r in rows]

    def waiting(self, destination):
        with self.connect() as db:
            rows = db.execute('''SELECT j.video_id FROM jobs j JOIN deliveries d
                ON j.video_id=d.video_id WHERE j.status IN ('pending','failed') AND
                d.destination=? AND d.output IS NULL ORDER BY j.updated''', (destination,)).fetchall()
        return [self.get(r['video_id']) for r in rows]

    def forget(self, vid):
        with self.connect() as db:
            db.execute('DELETE FROM deliveries WHERE video_id=?', (video_id(vid),))
            db.execute('DELETE FROM jobs WHERE video_id=?', (video_id(vid),))

    def drop_waiting(self, is_expired):
        """Remove jobs still waiting on playback once is_expired(payload) is true."""
        with self.connect() as db:
            rows = db.execute("SELECT video_id,payload FROM jobs "
                              "WHERE status IN ('pending','failed')").fetchall()
        expired = [r['video_id'] for r in rows if is_expired(json.loads(r['payload']))]
        for vid in expired:
            self.forget(vid)
        return len(expired)

    def recover_with_captions(self, vid, text, destination, output, source=None):
        """Close a waiting job once captions worked after all; no playback is needed."""
        with self.connect() as db:
            source = source or acquisition(API_SOURCE, engine='youtube-transcript-api')
            db.execute("""UPDATE jobs SET status='ready',transcript=?,model=?,error=NULL,updated=?,
                acquisition=?,next_action='deliver'
                WHERE video_id=? AND status IN ('pending','failed')""",
                (text.strip(), CAPTION_MODEL, time.time(), json.dumps(source, ensure_ascii=False), video_id(vid)))
            db.execute('''INSERT INTO deliveries(video_id,destination,output) VALUES(?,?,?)
                ON CONFLICT(video_id,destination) DO UPDATE SET output=excluded.output''',
                       (video_id(vid), destination, str(output)))

    def import_ui(self, vid, document):
        """Import captions without pretending a recording took place or delivering early."""
        text, source = validate_ui_document(video_id(vid), document)
        with self.connect() as db:
            changed = db.execute("""UPDATE jobs SET status='ready',transcript=?,model=?,
                acquisition=?,next_action='deliver',error=NULL,updated=?
                WHERE video_id=? AND status IN ('pending','failed')""",
                (text, CAPTION_MODEL, json.dumps(source, ensure_ascii=False), time.time(), vid)).rowcount
            if changed != 1:
                raise ValueError('UI import needs a pending/failed job; completed input is preserved')
        return self.get(vid)

    def ui_unavailable(self, vid, reason):
        with self.connect() as db:
            changed = db.execute("""UPDATE jobs SET next_action='playback',error=?,updated=?
                WHERE video_id=? AND status IN ('pending','failed')""",
                (str(reason), time.time(), video_id(vid))).rowcount
            if changed != 1:
                raise ValueError('UI fallback needs a pending/failed job')

    def retry_ui(self, vid):
        with self.connect() as db:
            changed = db.execute("""UPDATE jobs SET next_action='ui_transcript',error=NULL,updated=?
                WHERE video_id=? AND status IN ('pending','failed')""",
                (time.time(), video_id(vid))).rowcount
            if changed != 1:
                raise ValueError('UI retry needs a pending/failed job')

    def needs_delivery(self, vid, destination):
        with self.connect() as db:
            row = db.execute('SELECT output FROM deliveries WHERE video_id=? AND destination=?',
                             (video_id(vid), destination)).fetchone()
            return row is not None and row['output'] is None

    def delivered(self, vid, destination, output):
        with self.connect() as db:
            db.execute('''INSERT INTO deliveries(video_id,destination,output) VALUES(?,?,?)
                ON CONFLICT(video_id,destination) DO UPDATE SET output=excluded.output''',
                       (video_id(vid), destination, str(output)))

    def claim(self, vid):
        with self.connect() as db:
            changed = db.execute('''UPDATE jobs SET status='recording',started=?,updated=?,
                error=NULL,evidence='[]',next_action='playback'
                WHERE video_id=? AND status IN ('pending','failed')''',
                (time.time(), time.time(), video_id(vid))).rowcount
            if changed != 1:
                raise ValueError('Job is missing, already recording, or already transcribed')

    def started(self, vid, timestamp):
        with self.connect() as db:
            db.execute("UPDATE jobs SET started=? WHERE video_id=? AND status='recording'",
                       (timestamp, video_id(vid)))

    def observe(self, vid, sample):
        # A browser-read timestamp avoids counting tool-delivery latency as a
        # seek. Only fresh observations are accepted; no post-hoc backfilling.
        now = time.time()
        captured = float(sample.get('captured_at', now))
        if not math.isfinite(captured) or not -1 <= now - captured <= 15:
            raise ValueError('Player observation must be submitted within 15 seconds')
        sample = dict(sample, observed_at=captured)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT status,evidence FROM jobs WHERE video_id=?', (video_id(vid),)).fetchone()
            if not row or row['status'] != 'recording':
                raise ValueError('Observations require an active recording')
            samples = json.loads(row['evidence'])
            if samples and captured <= samples[-1]['observed_at']:
                raise ValueError('Player observations must be ordered')
            samples.append(sample)
            db.execute('UPDATE jobs SET evidence=?,updated=? WHERE video_id=?',
                       (json.dumps(samples), time.time(), vid))

    def fail(self, vid, error):
        with self.connect() as db:
            db.execute("UPDATE jobs SET status='failed',error=?,updated=? WHERE video_id=?",
                       (str(error), time.time(), video_id(vid)))

    def finish(self, vid, text, model):
        if len(text.strip()) < 200:
            raise ValueError('Transcript too short; keeping job retryable')
        with self.connect() as db:
            source = acquisition(LOCAL_SOURCE, engine='whisper-cpp', model=model,
                                 language='ko', generated=True)
            changed = db.execute("""UPDATE jobs SET status='ready',transcript=?,model=?,updated=?,
                acquisition=?,next_action='deliver'
                WHERE video_id=? AND status='recording'""",
                (text.strip(), model, time.time(), json.dumps(source), video_id(vid))).rowcount
            if changed != 1:
                raise ValueError('Recording is no longer active')


def validate_playback(job, recorded_seconds, finished_at):
    """Reject incomplete/ad-contaminated/unsupervised capture before local STT.

    Samples must be read from the actual player by the browser controller. This
    is sampled evidence, not continuous ad detection or proof of content rights.
    Returns estimated audio offset and verified content duration.
    """
    samples = job['evidence']
    if len(samples) < 2:
        raise ValueError('Need playback start, progress, and end observations')
    duration = float(samples[0]['duration'])
    if not 1 <= duration <= 21600:
        raise ValueError('Finite, non-live video duration required')
    if samples[0]['current_time'] > 2 or not samples[-1]['ended']:
        raise ValueError('Playback must start at zero and reach ended')
    if samples[-1]['current_time'] < duration - 2:
        raise ValueError('Playback ended before the full content was captured')
    if not 0 <= samples[0]['observed_at'] - job['started'] <= 30:
        raise ValueError('Playback start was not observed during capture startup')
    if not 0 <= finished_at - samples[-1]['observed_at'] <= 15:
        raise ValueError('Missing recent playback end observation')
    previous = None
    for s in samples:
        if not all(math.isfinite(float(s[k])) for k in ('current_time', 'duration', 'rate', 'observed_at')):
            raise ValueError('Non-finite player evidence')
        if s['video_id'] != job['video_id'] or s['ad'] is not False or s['muted'] is not False:
            raise ValueError('Wrong video, advertisement, or muted player')
        if s['rate'] != 1 or abs(s['duration'] - duration) > 1:
            raise ValueError('Playback speed or video duration changed')
        if (s['paused'] and not s['ended']) or s['buffering'] is not False:
            raise ValueError('Playback paused or buffering')
        if previous:
            wall = s['observed_at'] - previous['observed_at']
            progress = s['current_time'] - previous['current_time']
            expected_progress = min(wall, duration - previous['current_time']) if s['ended'] else wall
            if not 0 < wall <= 35 or abs(expected_progress - progress) > 3:
                raise ValueError('Observation gap, seek, or interrupted playback')
        previous = s
    offset = samples[0]['observed_at'] - job['started'] - samples[0]['current_time']
    if offset < -2 or recorded_seconds < max(0, offset) + duration - 2:
        raise ValueError('Recorded audio is shorter than verified playback')
    return max(0, offset), duration


def interrupted(sample):
    """Ad, stall or unexpected pause: content stops while the recorder keeps running."""
    return (sample['ad'] is not False or sample['buffering'] is not False
            or (sample['paused'] and not sample['ended']))


def playback_segments(job, recorded_seconds, finished_at):
    """Audio ranges holding the content when ads or stalls interrupt automated playback.

    Same evidence rules as validate_playback, except that an interruption is cut out
    instead of rejecting the recording. Content must resume where it stopped, so a
    seek is still rejected. Cuts sit on real content observations, which drops up to
    one sampling interval of speech per interruption but never keeps ad audio.
    Returns [(audio_start, audio_end), ...] and the verified content duration.
    """
    samples = job['evidence']
    content = [s for s in samples if not interrupted(s)]
    if len(content) < 2:
        raise ValueError('Need playback start, progress, and end observations')
    duration = float(content[0]['duration'])
    if not 1 <= duration <= 21600:
        raise ValueError('Finite, non-live video duration required')
    if content[0]['current_time'] > 2 or not samples[-1]['ended'] or interrupted(samples[-1]):
        raise ValueError('Playback must start at zero and reach ended')
    if samples[-1]['current_time'] < duration - 2:
        raise ValueError('Playback ended before the full content was captured')
    if not 0 <= samples[0]['observed_at'] - job['started'] <= 30:
        raise ValueError('Playback start was not observed during capture startup')
    if not 0 <= finished_at - samples[-1]['observed_at'] <= 15:
        raise ValueError('Missing recent playback end observation')
    runs, previous = [[]], None
    for s in samples:
        # A loading or ad player can report NaN; only content samples need a real clock.
        numbers = ('observed_at',) if interrupted(s) else ('current_time', 'duration', 'rate', 'observed_at')
        if not all(math.isfinite(float(s[k])) for k in numbers):
            raise ValueError('Non-finite player evidence')
        if s['video_id'] != job['video_id']:
            raise ValueError('Wrong video')
        if previous and not 0 < s['observed_at'] - previous['observed_at'] <= 35:
            raise ValueError('Observation gap')
        previous = s
        if interrupted(s):
            if runs[-1]:
                runs.append([])
            continue
        if s['muted'] is not False or s['rate'] != 1 or abs(s['duration'] - duration) > 1:
            raise ValueError('Muted player, playback speed or video duration changed')
        runs[-1].append(s)
    runs = [run for run in runs if run]
    for run in runs:
        for before, s in zip(run, run[1:]):
            wall = s['observed_at'] - before['observed_at']
            progress = s['current_time'] - before['current_time']
            expected_progress = min(wall, duration - before['current_time']) if s['ended'] else wall
            if abs(expected_progress - progress) > 3:
                raise ValueError('Seek or interrupted playback')
    for before, run in zip(runs, runs[1:]):
        if not -1 <= run[0]['current_time'] - before[-1]['current_time'] <= 3:
            raise ValueError('Content did not resume where it stopped')
        if run[0]['observed_at'] - before[-1]['observed_at'] > 900:
            raise ValueError('Interruption too long')
    segments = []
    for index, run in enumerate(runs):
        # The ended sample can arrive after the player clock stopped; leave it out.
        timing = [s for s in run if not s['ended']] or run
        offset = statistics.median(s['observed_at'] - job['started'] - s['current_time'] for s in timing)
        if offset < -2:
            raise ValueError('Recorded audio is shorter than verified playback')
        start = 0 if index == 0 else run[0]['current_time']
        end = duration if index == len(runs) - 1 else run[-1]['current_time']
        if end - start <= 0.05:
            continue
        if segments and max(0, start + offset) < segments[-1][1] - 0.5:
            raise ValueError('Inconsistent playback timing')
        segments.append((max(0, start + offset), end + offset))
    if recorded_seconds < segments[-1][1] - 2:
        raise ValueError('Recorded audio is shorter than verified playback')
    return segments, duration

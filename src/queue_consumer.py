"""Queue consumer: NarrativeRadar work-queue → contract JSON inbox.

This is the bridge that lets NarrativeRadar (via DataConductor) discover videos
and hand them here for transcript + summary, then receive the result as a
contract JSON it can ingest into `narratives.raw_messages`.

Flow:
  [DataConductor] youtube discover  → queue_dir/pending_<date>.jsonl
  [this module]   read queue → transcript (free captions first; direct-media
                                audio + Whisper as the auxiliary fallback)
                              → summary (Summarizer)
                              → inbox_dir/<video_id>.json   (contract schema)
  [DataConductor] collect youtube   → raw_messages

Contract schema: DataConductor/docs is the canonical copy; required output
fields are video_id, published_at, summary. Captions and audio STT are never
both used for one successful video.

Shared folders are resolved from the SAME env vars DataConductor uses
(`NARRATIVES_YOUTUBE_QUEUE_DIR` / `NARRATIVES_YOUTUBE_INBOX_DIR`) so a single
location works across both repos without config drift.
"""

import html
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from utils import setup_logging
from state_manager import StateManager
from summarizer import Summarizer
from realtime_fallback import CAPTION_MODEL, PlaybackQueue, enabled as realtime_enabled
from transcript_acquisition import (UI_SOURCE, LOCAL_SOURCE, from_extractor,
                                    from_job, review_summary)

logger = setup_logging("queue_consumer")

# Default shared location = a OneDrive folder both repos can reach. Override via
# env on either side to relocate; both repos read these identical vars.
_DEFAULT_QUEUE = r"D:\OneDrive\Information\Explorer_Stock\youtube_queue"
_DEFAULT_INBOX = r"D:\OneDrive\Information\Explorer_Stock\youtube_inbox"

# Drop videos older than this (real published_at, from the official Data API
# discovery row). 0 = off.
_MAX_AGE_DAYS = int(os.getenv("NARRATIVES_YOUTUBE_MAX_AGE_DAYS", "30"))

# Politeness delay between transcript fetches. The transcript path still uses
# youtube-transcript-api (the only way to read third-party captions; the official
# Data API can't), which IP-rate-limits on bursts — discovery moved to the
# official API, but bulk transcript pulls must still be throttled. Tune via env.
_REQUEST_DELAY = float(os.getenv("NARRATIVES_YOUTUBE_REQUEST_DELAY", "5.0"))

# Transcript backend: captions-first (default), captions-only, or whisper-only.
# The default lazily constructs Whisper when a video genuinely has no captions
# or when only the public-caption endpoint is IP-blocked. The auxiliary backend
# downloads a direct media resource and passes only its audio onward, so
# YouTube-inserted ads and player buffering are not recorded. Generic errors and
# unavailable videos do not trigger paid STT.
_TRANSCRIBER = os.getenv("NARRATIVES_YOUTUBE_TRANSCRIBER", "captions-first").lower()


def _make_caption_transcriber():
    from transcript_extractor import TranscriptExtractor
    return TranscriptExtractor()


def _make_whisper_transcriber():
    from whisper_transcript import WhisperTranscriber
    return WhisperTranscriber()


class _CaptionsFirstTranscriber:
    """Free captions with lazy direct-media STT as an auxiliary fallback."""

    def __init__(self):
        self._captions = _make_caption_transcriber()
        self._whisper = None
        self.fallback_reason = None
        self._sync_metadata(self._captions)

    def _sync_metadata(self, source):
        self.engine = getattr(source, "engine", "youtube-transcript-api")
        self.model = getattr(source, "model", None)
        self.last_track_info = getattr(source, "last_track_info", None)
        self.last_segments = getattr(source, "last_segments", None)

    def extract_with_status(self, video_id):
        self.fallback_reason = None
        text, status = self._captions.extract_with_status(video_id)
        self._sync_metadata(self._captions)
        if status not in {"no_captions", "blocked"}:
            return text, status

        self.fallback_reason = f"captions_{status}"
        logger.info(
            "queue: %s → direct-media/Whisper auxiliary fallback for %s",
            status,
            video_id,
        )
        if self._whisper is None:
            self._whisper = _make_whisper_transcriber()
        text, status = self._whisper.extract_with_status(video_id)
        self._sync_metadata(self._whisper)
        return text, status


def _make_transcriber(mode=None):
    mode = (mode or _TRANSCRIBER).lower()
    if mode == "captions-first":
        return _CaptionsFirstTranscriber()
    if mode == "captions":
        return _make_caption_transcriber()
    if mode == "whisper":
        return _make_whisper_transcriber()
    raise ValueError(
        "NARRATIVES_YOUTUBE_TRANSCRIBER must be captions-first, captions, or whisper"
    )


def _too_old(published_at_iso: str) -> bool:
    if _MAX_AGE_DAYS <= 0:
        return False
    try:
        # Python 3.10's fromisoformat rejects the "Z" suffix the Data API returns.
        pub = datetime.fromisoformat(published_at_iso.replace("Z", "+00:00"))
    except ValueError:
        return False
    if pub.tzinfo is None:
        pub = pub.replace(tzinfo=timezone.utc)
    return pub < datetime.now(timezone.utc) - timedelta(days=_MAX_AGE_DAYS)


def drop_expired_jobs(playback) -> None:
    """Videos past the age cap get no fallback; captured transcripts stay deliverable."""
    dropped = playback.drop_waiting(lambda payload: _too_old(payload.get("published_at") or ""))
    if dropped:
        logger.info("queue: dropped %d playback jobs past the age cap", dropped)


def _queue_dir() -> Path:
    p = Path(os.getenv("NARRATIVES_YOUTUBE_QUEUE_DIR", _DEFAULT_QUEUE))
    p.mkdir(parents=True, exist_ok=True)
    return p


def _inbox_dir() -> Path:
    p = Path(os.getenv("NARRATIVES_YOUTUBE_INBOX_DIR", _DEFAULT_INBOX))
    p.mkdir(parents=True, exist_ok=True)
    return p


def _read_queue(queue_dir: Path) -> list[dict]:
    """Read + dedup every pending_*.jsonl row (latest occurrence wins)."""
    by_id: dict[str, dict] = {}
    for qfile in sorted(queue_dir.glob("pending_*.jsonl")):
        for line in qfile.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("queue: bad jsonl line in %s", qfile.name)
                continue
            vid = row.get("video_id")
            if vid:
                by_id[vid] = row
    return list(by_id.values())


def _newest_first(rows: list[dict]) -> list[dict]:
    """A caption block ends a run after a few requests; spend them on fresh videos."""
    def published(row: dict) -> datetime:
        try:
            pub = datetime.fromisoformat((row.get("published_at") or "").replace("Z", "+00:00"))
        except ValueError:
            return datetime.min.replace(tzinfo=timezone.utc)
        return pub if pub.tzinfo else pub.replace(tzinfo=timezone.utc)
    return sorted(rows, key=published, reverse=True)


def _combined_summary(sections: dict) -> str:
    """Flatten the section dict into a single Korean text blob for raw_messages."""
    parts = []
    if sections.get("overview"):
        parts.append("[개요]\n" + sections["overview"].strip())
    if sections.get("key_points"):
        parts.append("[핵심 포인트]\n" + sections["key_points"].strip())
    if sections.get("quotes"):
        parts.append("[주요 인용구]\n" + sections["quotes"].strip())
    if sections.get("takeaways"):
        parts.append("[핵심 요점]\n" + sections["takeaways"].strip())
    if not parts:
        # Parser produced nothing structured — fall back to the raw response.
        return (sections.get("raw_response") or "").strip()
    return "\n\n".join(parts)


def _write_contract(inbox_dir: Path, doc: dict) -> Path:
    out = inbox_dir / f"{doc['video_id']}.json"
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(out)  # atomic: ingest never sees a half-written file
    return out


def candidates(limit: int = 30) -> dict:
    """Newest unprocessed in-window videos, with length, for the daily playback offer."""
    from youtube_api import YouTubeAPI

    state = StateManager()
    playback = PlaybackQueue()
    rows = []
    for row in _newest_first(_read_queue(_queue_dir())):
        vid = row["video_id"]
        job = playback.get(vid)
        if state.is_video_processed(vid) or (job and job["status"] in ("ready", "recording")):
            continue
        if _too_old(row.get("published_at") or ""):
            continue
        rows.append(row)
    # Look up a margin beyond the limit: deleted, private and live videos drop out.
    details = YouTubeAPI().get_video_durations([row["video_id"] for row in rows[: limit * 2]])
    items = []
    for row in rows[: limit * 2]:
        info = details.get(row["video_id"])
        if not info or info["live"] or not info["duration_seconds"]:
            continue
        items.append({
            "video_id": row["video_id"],
            # Data API snippets arrive HTML-escaped (&#39;, &quot;).
            "title": html.unescape(row.get("title") or row["video_id"]),
            "channel_title": row.get("channel_title") or "youtube",
            "channel_id": row.get("channel_id"),
            "published_at": row.get("published_at"),
            "url": row.get("url") or f"https://youtu.be/{row['video_id']}",
            "duration_seconds": info["duration_seconds"],
        })
        if len(items) == limit:
            break
    return {"in_window": len(rows), "items": items}


def run(limit: int | None = None, force: bool = False, only: set[str] | None = None) -> dict:
    """Process the queue end-to-end. Returns a result summary dict."""
    queue_dir = _queue_dir()
    inbox_dir = _inbox_dir()
    state = StateManager()
    playback = PlaybackQueue() if realtime_enabled() and _TRANSCRIBER == 'captions-first' else None
    extractor = _make_caption_transcriber() if playback else _make_transcriber()
    logger.info("queue: transcript backend = %s (engine=%s)", _TRANSCRIBER, getattr(extractor, "engine", "?"))
    if playback:
        drop_expired_jobs(playback)
    summarizer: Summarizer | None = None

    rows = _read_queue(queue_dir)
    if playback:
        seen = {row['video_id'] for row in rows}
        rows += [job['payload'] for job in playback.ready('narrative') if job['video_id'] not in seen]
    rows = _newest_first(rows)
    if playback:
        # A recovered transcript needs no YouTube request, so deliver it before
        # --limit is spent on caption fetches; otherwise newer uploads keep
        # pushing it back. The sort is stable, so the rest stay newest-first.
        recovered = {job['video_id'] for job in playback.ready('narrative')}
        rows.sort(key=lambda row: row['video_id'] not in recovered)
    if only is not None:
        rows = [row for row in rows if row["video_id"] in only]
    result = {"queued": len(rows), "written": 0, "skipped": 0,
              "no_transcript": 0, "unavailable": 0, "too_old": 0,
              "caption_used": 0, "whisper_used": 0, "realtime_used": 0, "ui_used": 0, "deferred": 0,
              "errors": 0, "blocked": False}
    logger.info("queue: %d unique videos pending", len(rows))

    processed_count = 0
    blocked = False
    stopped_at_limit = False
    for row in rows:
        vid = row["video_id"]

        existing_job = playback.get(vid) if playback else None
        if existing_job and not (inbox_dir / f'{vid}.json').exists():
            playback.enqueue(row, existing_job['reason'], 'narrative')
        pending_delivery = playback and playback.needs_delivery(vid, 'narrative')
        if not force and state.is_video_processed(vid) and not pending_delivery:
            result["skipped"] += 1
            continue
        if (inbox_dir / f"{vid}.json").exists():
            result["skipped"] += 1
            continue
        if limit is not None and processed_count >= limit:
            stopped_at_limit = True
            break

        # Metadata comes straight from the official Data API discovery row
        # (published_at / channel / title) — no per-video scraping needed.
        published_at = row.get("published_at")
        channel_title = row.get("channel_title") or "youtube"
        channel_id = row.get("channel_id")
        title = row.get("title") or vid
        url = row.get("url") or f"https://youtu.be/{vid}"

        if not published_at:
            # Required by the contract; without it the ingest side would DLQ.
            # Use processing time as a last resort but log loudly — this can
            # mis-bucket the Emergence phase, so it should be rare.
            published_at = datetime.now(timezone.utc).isoformat()
            logger.warning("queue: no published_at for %s; using now() (phase risk)", vid)
        elif _too_old(published_at) and not pending_delivery:
            # Defensive age cap (discovery already filters via publishedAfter, but
            # a stale queue file could still carry old rows). Mark processed so the
            # same old video isn't re-summarized every run.
            logger.info("queue: %s too old (%s) — skipping", vid, published_at[:10])
            state.mark_video_processed(vid, title, channel_title, summary_file="", published_date=published_at[:10])
            result["too_old"] += 1
            continue

        job = playback.get(vid) if playback else None
        if job and job['status'] == 'ready':
            transcript, status = job['transcript'], 'ok'
        elif job and job['status'] == 'recording':
            transcript, status = None, 'deferred'
        else:
            # Waiting jobs retry captions too: a block or a missing track can clear
            # on its own, and manual playback may never happen.
            if _REQUEST_DELAY > 0:
                time.sleep(_REQUEST_DELAY)  # throttle transcript requests to avoid IP rate-limit
            transcript, status = extractor.extract_with_status(vid)
        if playback and status in {'blocked', 'no_captions', 'deferred'}:
            playback.enqueue(dict(row, title=title, channel_title=channel_title,
                                  published_at=published_at, url=url),
                             f'captions_{status}', 'narrative')
            result['deferred'] += 1
            if status == 'blocked':
                # Every later fetch would be blocked too. Stop so the rest stay queued
                # for a caption retry instead of all moving to manual playback.
                logger.warning("queue: YouTube IP-blocked at %s — aborting run", vid)
                blocked = True
                break
            # A job that was already waiting must not use up --limit and starve the
            # videos queued behind it.
            if job is None:
                processed_count += 1
            continue
        if status == "blocked":
            # YouTube rate-limited us. Every subsequent fetch will fail too, and
            # marking them "processed" would silently drop them. Stop now and
            # leave the rest of the queue intact for a later retry.
            logger.warning("queue: YouTube IP-blocked at %s — aborting run, %d left", vid, len(rows) - processed_count)
            blocked = True
            break
        if status == "no_captions":
            # Genuinely no transcript — permanent, mark done so we don't retry.
            logger.info("queue: no captions for %s — marking processed (skip)", vid)
            state.mark_video_processed(vid, title, channel_title, summary_file="", published_date=published_at[:10])
            result["no_transcript"] += 1
            continue
        if status == "unavailable":
            logger.info("queue: video unavailable for %s — marking processed (skip)", vid)
            state.mark_video_processed(
                vid, title, channel_title, summary_file="", published_date=published_at[:10]
            )
            if job:
                playback.forget(vid)
            result["unavailable"] += 1
            continue
        if status == "error" or not transcript:
            # Transient non-block failure — do NOT mark processed; retry next run.
            logger.warning("queue: transient transcript error for %s — will retry", vid)
            result["errors"] += 1
            continue

        if summarizer is None:
            summarizer = Summarizer()
        sections = summarizer.summarize(transcript=transcript, title=title, channel=channel_title)
        if not sections:
            logger.error("queue: summarization failed for %s", vid)
            result["errors"] += 1
            continue

        summary_text = _combined_summary(sections)
        doc = {
            "video_id": vid,
            "url": url,
            "channel_id": channel_id,
            "channel_title": channel_title,
            "title": title,
            "published_at": published_at,
            "summary": summary_text,
            "discovery": row.get("discovery") or {"mode": "unknown", "query": None},
            "stt": {
                "engine": getattr(extractor, "engine", "youtube-transcript-api"),
                "model": getattr(extractor, "model", None),
                "lang": (getattr(extractor, "last_track_info", None) or {}).get(
                    "language", "ko"
                ),
                "generated": (getattr(extractor, "last_track_info", None) or {}).get(
                    "generated"
                ),
                "acquisition": (
                    "direct_media"
                    if getattr(extractor, "engine", "") == "whisper"
                    else "captions"
                ),
                "fallback_reason": getattr(extractor, "fallback_reason", None),
            },
            "summary_meta": {
                "source_char_len": len(transcript),
                "summary_char_len": len(summary_text),
            },
        }
        source = from_job(job) if job and job['status'] == 'ready' else from_extractor(extractor)
        played_back = source['source'] == LOCAL_SOURCE
        doc['stt'] = {'engine': source['engine'], 'model': source['model'],
                      'lang': source['language'], 'generated': source['generated'],
                      'acquisition': ('realtime_playback' if played_back else 'captions_ui'
                                      if source['source'] == UI_SOURCE else doc['stt']['acquisition']),
                      'fallback_reason': job['reason'] if job else doc['stt']['fallback_reason']}
        doc['summary_meta']['review'] = review_summary(transcript, sections)
        doc['summary_meta']['transcript_source'] = source['source']
        segments = source['segments']
        if segments:
            import hashlib
            captured_at = source['evidence'].get('captured_at')
            observed_at = (datetime.fromtimestamp(captured_at, timezone.utc).isoformat()
                           if isinstance(captured_at, (int, float)) else datetime.now(timezone.utc).isoformat())
            body = {"version": ('youtube-visible-transcript-v1' if source['source'] == UI_SOURCE
                                else 'youtube-timed-caption-v1'), "video_id": vid,
                    "channel_id": channel_id, "published_at": published_at,
                    "observed_at": observed_at,
                    "language": doc["stt"]["lang"], "generated": doc["stt"]["generated"],
                    "segments": segments, "acquisition": doc['stt']['acquisition']}
            encoded = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
            doc["transcript_evidence"] = dict(body, sha256=hashlib.sha256(encoded.encode()).hexdigest())
        _write_contract(inbox_dir, doc)
        if job and job['status'] == 'ready':
            playback.delivered(vid, 'narrative', inbox_dir / f'{vid}.json')
        elif job:
            playback.recover_with_captions(vid, transcript, 'narrative', inbox_dir / f'{vid}.json', source)
        state.mark_video_processed(vid, title, channel_title, summary_file=f"{vid}.json", published_date=published_at[:10])
        result["written"] += 1
        if played_back:
            result['realtime_used'] += 1
        elif source['source'] == UI_SOURCE:
            result['ui_used'] += 1
        elif getattr(extractor, "engine", "") == "whisper":
            result["whisper_used"] += 1
        else:
            result["caption_used"] += 1
        if not (job and job['status'] == 'ready'):
            # --limit rations YouTube requests; a recovered transcript made none.
            processed_count += 1
        logger.info("queue: wrote contract for %s (%s)", vid, title[:40])

    result["blocked"] = blocked

    # Archive consumed queue files so they are not re-read each run, but only if
    # this run reached the end of the deduplicated queue and left no retryable
    # work. A run that actually hit its limit, was IP-blocked, or saw transient
    # errors keeps the files for the next pass. Once a limit-bounded backlog is
    # fully drained, the final short run can still archive it.
    if (only is None and not stopped_at_limit and not blocked
            and result["errors"] == 0 and result["deferred"] == 0):
        archive = queue_dir / "_archive"
        archive.mkdir(parents=True, exist_ok=True)
        for qfile in queue_dir.glob("pending_*.jsonl"):
            try:
                qfile.rename(archive / qfile.name)
            except OSError as e:
                logger.warning("queue: could not archive %s: %s", qfile.name, e)

    logger.info(
        "queue done: written=%d captions=%d whisper=%d deferred=%d skipped=%d "
        "no_captions=%d unavailable=%d errors=%d blocked=%s",
        result["written"], result["caption_used"], result["whisper_used"],
        result["deferred"], result["skipped"], result["no_transcript"],
        result["unavailable"], result["errors"], blocked,
    )
    return result

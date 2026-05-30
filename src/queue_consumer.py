"""Queue consumer: NarrativeRadar work-queue → contract JSON inbox.

This is the bridge that lets NarrativeRadar (via DataConductor) discover videos
and hand them here for transcript + summary, then receive the result as a
contract JSON it can ingest into `narratives.raw_messages`.

Flow:
  [DataConductor] youtube discover  → queue_dir/pending_<date>.jsonl
  [this module]   read queue → transcript (Whisper STT by default; see
                                _make_transcriber) → summary (Summarizer)
                              → inbox_dir/<video_id>.json   (contract schema)
  [DataConductor] collect youtube   → raw_messages

Contract schema: DataConductor/docs is the canonical copy; required output
fields are video_id, published_at, summary. We reuse the existing
TranscriptExtractor + Summarizer so STT/summary cost is paid exactly once here.

Shared folders are resolved from the SAME env vars DataConductor uses
(`NARRATIVES_YOUTUBE_QUEUE_DIR` / `NARRATIVES_YOUTUBE_INBOX_DIR`) so a single
location works across both repos without config drift.
"""

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
_REQUEST_DELAY = float(os.getenv("NARRATIVES_YOUTUBE_REQUEST_DELAY", "1.5"))

# Transcript backend: "whisper" (audio download → OpenAI Whisper, avoids the
# caption-endpoint IP block) or "captions" (youtube-transcript-api). Default
# whisper. Both expose extract_with_status(video_id) -> (text, status).
_TRANSCRIBER = os.getenv("NARRATIVES_YOUTUBE_TRANSCRIBER", "whisper").lower()


def _make_transcriber():
    if _TRANSCRIBER == "captions":
        from transcript_extractor import TranscriptExtractor
        t = TranscriptExtractor()
        # Older extractor has no engine/model attrs; default them for the contract.
        if not hasattr(t, "engine"):
            t.engine = "youtube-transcript-api"
        if not hasattr(t, "model"):
            t.model = None
        return t
    from whisper_transcript import WhisperTranscriber
    return WhisperTranscriber()


def _too_old(published_at_iso: str) -> bool:
    if _MAX_AGE_DAYS <= 0:
        return False
    try:
        pub = datetime.fromisoformat(published_at_iso)
    except ValueError:
        return False
    if pub.tzinfo is None:
        pub = pub.replace(tzinfo=timezone.utc)
    return pub < datetime.now(timezone.utc) - timedelta(days=_MAX_AGE_DAYS)


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


def run(limit: int | None = None, force: bool = False) -> dict:
    """Process the queue end-to-end. Returns a result summary dict."""
    queue_dir = _queue_dir()
    inbox_dir = _inbox_dir()
    state = StateManager()
    extractor = _make_transcriber()
    logger.info("queue: transcript backend = %s (engine=%s)", _TRANSCRIBER, getattr(extractor, "engine", "?"))
    summarizer: Summarizer | None = None

    rows = _read_queue(queue_dir)
    result = {"queued": len(rows), "written": 0, "skipped": 0,
              "no_transcript": 0, "too_old": 0, "errors": 0, "blocked": False}
    logger.info("queue: %d unique videos pending", len(rows))

    processed_count = 0
    blocked = False
    for row in rows:
        vid = row["video_id"]

        if not force and state.is_video_processed(vid):
            result["skipped"] += 1
            continue
        if (inbox_dir / f"{vid}.json").exists():
            result["skipped"] += 1
            continue
        if limit is not None and processed_count >= limit:
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
        elif _too_old(published_at):
            # Defensive age cap (discovery already filters via publishedAfter, but
            # a stale queue file could still carry old rows). Mark processed so the
            # same old video isn't re-summarized every run.
            logger.info("queue: %s too old (%s) — skipping", vid, published_at[:10])
            state.mark_video_processed(vid, title, channel_title, summary_file="", published_date=published_at[:10])
            result["too_old"] += 1
            continue

        if _REQUEST_DELAY > 0:
            time.sleep(_REQUEST_DELAY)  # throttle transcript requests to avoid IP rate-limit
        transcript, status = extractor.extract_with_status(vid)
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
            "stt": {"engine": getattr(extractor, "engine", "youtube-transcript-api"),
                    "model": getattr(extractor, "model", None), "lang": "ko"},
            "summary_meta": {
                "source_char_len": len(transcript),
                "summary_char_len": len(summary_text),
            },
        }
        _write_contract(inbox_dir, doc)
        state.mark_video_processed(vid, title, channel_title, summary_file=f"{vid}.json", published_date=published_at[:10])
        result["written"] += 1
        processed_count += 1
        logger.info("queue: wrote contract for %s (%s)", vid, title[:40])

    result["blocked"] = blocked

    # Archive consumed queue files so they are not re-read each run — but ONLY
    # on a full, un-blocked run. A limit-bounded or IP-blocked run leaves
    # unprocessed rows behind, so we keep the queue files for the next pass
    # (already-done videos are skipped cheaply via StateManager / inbox).
    if limit is None and not blocked:
        archive = queue_dir / "_archive"
        archive.mkdir(parents=True, exist_ok=True)
        for qfile in queue_dir.glob("pending_*.jsonl"):
            try:
                qfile.rename(archive / qfile.name)
            except OSError as e:
                logger.warning("queue: could not archive %s: %s", qfile.name, e)

    logger.info(
        "queue done: written=%d skipped=%d no_captions=%d errors=%d blocked=%s",
        result["written"], result["skipped"], result["no_transcript"], result["errors"], blocked,
    )
    return result

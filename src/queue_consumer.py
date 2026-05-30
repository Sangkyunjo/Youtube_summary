"""Queue consumer: NarrativeRadar work-queue → contract JSON inbox.

This is the bridge that lets NarrativeRadar (via DataConductor) discover videos
and hand them here for transcript + summary, then receive the result as a
contract JSON it can ingest into `narratives.raw_messages`.

Flow:
  [DataConductor] youtube discover  → queue_dir/pending_<date>.jsonl
  [this module]   read queue → transcript (TranscriptExtractor)
                              → summary (Summarizer)
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
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from utils import setup_logging
from state_manager import StateManager
from transcript_extractor import TranscriptExtractor
from summarizer import Summarizer

logger = setup_logging("queue_consumer")

# Default shared location = a OneDrive folder both repos can reach. Override via
# env on either side to relocate; both repos read these identical vars.
_DEFAULT_QUEUE = r"D:\OneDrive\Information\Explorer_Stock\youtube_queue"
_DEFAULT_INBOX = r"D:\OneDrive\Information\Explorer_Stock\youtube_inbox"


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


def _fetch_video_meta(video_id: str) -> dict:
    """Resolve real published_at / channel / title via yt-dlp (single video).

    Flat search rows often lack the upload date, which the contract requires.
    Returns {} on failure; caller falls back to whatever the queue row had.
    """
    try:
        from yt_dlp import YoutubeDL
    except ImportError:
        logger.warning("yt-dlp not installed; cannot enrich video metadata")
        return {}

    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "ignoreerrors": True}
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"https://youtu.be/{video_id}", download=False)
    except Exception as e:  # noqa: BLE001
        logger.warning("yt-dlp meta failed for %s: %s", video_id, e)
        return {}
    if not info:
        return {}

    published_at = None
    ts = info.get("timestamp")
    if ts:
        published_at = datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
    elif info.get("upload_date") and len(str(info["upload_date"])) == 8:
        try:
            published_at = (
                datetime.strptime(str(info["upload_date"]), "%Y%m%d")
                .replace(tzinfo=timezone.utc)
                .isoformat()
            )
        except ValueError:
            published_at = None

    return {
        "published_at": published_at,
        "channel_id": info.get("channel_id") or info.get("uploader_id"),
        "channel_title": info.get("channel") or info.get("uploader"),
        "title": info.get("title"),
        "url": info.get("webpage_url"),
    }


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
    extractor = TranscriptExtractor()
    summarizer: Summarizer | None = None

    rows = _read_queue(queue_dir)
    result = {"queued": len(rows), "written": 0, "skipped": 0, "no_transcript": 0, "errors": 0}
    logger.info("queue: %d unique videos pending", len(rows))

    processed_count = 0
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

        meta = _fetch_video_meta(vid)
        published_at = meta.get("published_at") or row.get("published_at")
        channel_title = meta.get("channel_title") or row.get("channel_title") or "youtube"
        channel_id = meta.get("channel_id") or row.get("channel_id")
        title = meta.get("title") or row.get("title") or vid
        url = meta.get("url") or row.get("url") or f"https://youtu.be/{vid}"

        if not published_at:
            # Required by the contract; without it the ingest side would DLQ.
            # Use processing time as a last resort but log loudly — this can
            # mis-bucket the Emergence phase, so it should be rare.
            published_at = datetime.now(timezone.utc).isoformat()
            logger.warning("queue: no published_at for %s; using now() (phase risk)", vid)

        transcript = extractor.extract(vid)
        if not transcript:
            logger.info("queue: no transcript for %s — marking processed (skip)", vid)
            state.mark_video_processed(vid, title, channel_title, summary_file="", published_date=published_at[:10])
            result["no_transcript"] += 1
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
            "stt": {"engine": "youtube-transcript-api", "model": None, "lang": "ko"},
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

    # Archive consumed queue files so they are not re-read each run — but ONLY
    # on a full run. A limit-bounded run leaves unprocessed rows behind, so we
    # keep the queue files for the next pass (already-done videos are skipped
    # cheaply via StateManager / inbox existence).
    if limit is None:
        archive = queue_dir / "_archive"
        archive.mkdir(parents=True, exist_ok=True)
        for qfile in queue_dir.glob("pending_*.jsonl"):
            try:
                qfile.rename(archive / qfile.name)
            except OSError as e:
                logger.warning("queue: could not archive %s: %s", qfile.name, e)

    logger.info(
        "queue done: written=%d skipped=%d no_transcript=%d errors=%d",
        result["written"], result["skipped"], result["no_transcript"], result["errors"],
    )
    return result

"""
State management for tracking processed videos and run history.
Uses JSON files for persistence.
"""

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from processing_lock import check_claim, exclusive_claim
from ytsum_utils import atomic_write_text, get_project_root, load_config, setup_logging

logger = setup_logging("state_manager")


def _legacy_destination(summary_file):
    if summary_file.endswith('.txt'):
        return 'daily'
    if not summary_file or summary_file.endswith('.json'):
        return 'narrative'
    return None


class StateManager:
    """Manages the state of processed videos and run history."""

    def __init__(self):
        config = load_config()
        self.project_root = get_project_root()
        self.state_file = self.project_root / config["paths"]["state_file"]
        self.last_run_file = self.project_root / config["paths"]["last_run_file"]

        # Ensure data directory exists
        self.state_file.parent.mkdir(parents=True, exist_ok=True)

        # Load state on initialization
        self.state = self._load_state()
        self.last_run = self._load_last_run()

    def _load_state(self) -> dict:
        """Load the processed videos state from file."""
        try:
            state = json.loads(self.state_file.read_text(encoding='utf-8'))
        except FileNotFoundError:
            state = {"processed_videos": {}, "video_metadata": {}}
        if 'processed_by_destination' not in state:
            records = state['processed_by_destination'] = {'daily': {}, 'narrative': {}}
            for vid, metadata in state.get('video_metadata', {}).items():
                if vid in state.get('processed_videos', {}) and isinstance(metadata.get('summary_file'), str):
                    destination = _legacy_destination(metadata['summary_file'])
                    if destination:
                        records[destination][vid] = metadata
        return state

    def refresh(self):
        self.state = self._load_state()

    def _claim_path(self, key: str) -> Path:
        return self.state_file.parent / '.processing-locks' / key

    def processing_claim(self, video_id: str, destination: str):
        key = hashlib.sha256(f'{destination}\0{video_id}'.encode('utf-8')).hexdigest()
        return exclusive_claim(self._claim_path(key))

    def _save_state(self, state=None):
        """Save the state to file."""
        atomic_write_text(self.state_file, json.dumps(self.state if state is None else state,
                                                     indent=2, ensure_ascii=False))

    def _load_last_run(self) -> dict:
        """Load the last run information from file."""
        if self.last_run_file.exists():
            try:
                with open(self.last_run_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError) as e:
                logger.warning(f"Error loading last run file: {e}")
        return {"last_run_time": None, "last_run_status": None}

    def _save_last_run(self):
        """Save the last run information to file."""
        try:
            atomic_write_text(self.last_run_file, json.dumps(self.last_run, indent=2, ensure_ascii=False))
        except IOError as e:
            logger.error(f"Error saving last run file: {e}")

    def is_video_processed(self, video_id: str, destination: str = None) -> bool:
        """Check if a video has already been processed."""
        if destination is not None:
            return self.get_video_metadata(video_id, destination) is not None
        return video_id in self.state.get("processed_videos", {})

    def mark_video_processed(
        self,
        video_id: str,
        title: str,
        channel: str,
        summary_file: str,
        published_date: str = None,
        *,
        destination: str = None,
    ):
        """Mark a video as processed and store its metadata."""
        timestamp = datetime.now().isoformat()
        metadata = {
            "title": title,
            "channel": channel,
            "summary_file": summary_file,
            "published_date": published_date,
            "processed_at": timestamp
        }
        destination = destination or _legacy_destination(summary_file)
        with exclusive_claim(self._claim_path('state'), wait=True, stale_after=120) as claim:
            state = self._load_state()
            state.setdefault('processed_videos', {})[video_id] = timestamp
            state.setdefault('video_metadata', {})[video_id] = metadata
            if destination:
                state['processed_by_destination'].setdefault(destination, {})[video_id] = metadata
            check_claim(claim)
            self._save_state(state)
            self.state = state
        logger.info(f"Marked video as processed: {video_id} - {title}")

    def get_video_metadata(self, video_id: str, destination: str = None) -> Optional[dict]:
        """Get metadata for a processed video."""
        return self._processed_records(destination).get(video_id)

    def _processed_records(self, destination):
        if destination is None:
            return self.state.get('video_metadata', {})
        return self.state.get('processed_by_destination', {}).get(destination, {})

    def get_recently_processed(self, since: datetime = None) -> list:
        """Get list of videos processed since a given time."""
        if since is None:
            # Default to videos processed in the last run
            last_run_time = self.get_last_run_time()
            if last_run_time:
                since = last_run_time
            else:
                return []

        result = []
        for video_id, metadata in self.state.get("video_metadata", {}).items():
            processed_at = datetime.fromisoformat(metadata["processed_at"])
            if processed_at >= since:
                result.append({
                    "video_id": video_id,
                    **metadata
                })

        return sorted(result, key=lambda x: x["processed_at"], reverse=True)

    def get_last_run_time(self) -> Optional[datetime]:
        """Get the timestamp of the last successful run."""
        last_run_str = self.last_run.get("last_run_time")
        if last_run_str:
            return datetime.fromisoformat(last_run_str)
        return None

    def get_last_run_status(self) -> Optional[str]:
        """Get the status of the last run."""
        return self.last_run.get("last_run_status")

    def record_run_start(self):
        """Record the start of a new run."""
        self.last_run["current_run_start"] = datetime.now().isoformat()
        self.last_run["last_run_status"] = "running"
        self._save_last_run()
        logger.info("Run started")

    def record_run_complete(self, videos_processed: int = 0, errors: int = 0, deferred: int = 0):
        """Record the completion of a run."""
        self.last_run["last_run_time"] = datetime.now().isoformat()
        self.last_run["last_run_status"] = ("completed_with_errors" if errors else
                                            "awaiting_playback" if deferred else "success")
        self.last_run.pop("last_run_error", None)
        self.last_run["last_run_videos_deferred"] = deferred
        self.last_run["last_run_videos_processed"] = videos_processed
        self.last_run["last_run_errors"] = errors
        self._save_last_run()
        logger.info(f"Run completed: {videos_processed} videos processed, {errors} errors")

    def record_run_failed(self, error_message: str):
        """Record a failed run."""
        self.last_run["last_run_time"] = datetime.now().isoformat()
        self.last_run["last_run_status"] = "failed"
        self.last_run["last_run_error"] = error_message
        self._save_last_run()
        logger.error(f"Run failed: {error_message}")

    def get_all_processed_video_ids(self, destination: str = None) -> set:
        """Get all processed video IDs."""
        if destination is not None:
            return set(self._processed_records(destination))
        return set(self.state.get("processed_videos", {}).keys())

    def cleanup_old_entries(self, days: int = 90):
        """Remove entries older than specified days to prevent state file bloat."""
        cutoff = datetime.now().timestamp() - (days * 24 * 60 * 60)

        with exclusive_claim(self._claim_path('state'), wait=True, stale_after=120) as claim:
            state = self._load_state()
            to_remove = []
            for video_id, timestamp_str in state.get("processed_videos", {}).items():
                try:
                    timestamp = datetime.fromisoformat(timestamp_str).timestamp()
                    if timestamp < cutoff:
                        to_remove.append(video_id)
                except (ValueError, TypeError):
                    continue

            for video_id in to_remove:
                state["processed_videos"].pop(video_id, None)
                state.get("video_metadata", {}).pop(video_id, None)
                for records in state['processed_by_destination'].values():
                    records.pop(video_id, None)

            if to_remove:
                check_claim(claim)
                self._save_state(state)
                logger.info(f"Cleaned up {len(to_remove)} old entries from state")
            self.state = state

"""
State management for tracking processed videos and run history.
Uses JSON files for persistence.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from utils import get_project_root, load_config, setup_logging

logger = setup_logging("state_manager")


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
        if self.state_file.exists():
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError) as e:
                logger.warning(f"Error loading state file, starting fresh: {e}")
        return {"processed_videos": {}, "video_metadata": {}}

    def _save_state(self):
        """Save the state to file."""
        try:
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump(self.state, f, indent=2, ensure_ascii=False)
        except IOError as e:
            logger.error(f"Error saving state file: {e}")

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
            with open(self.last_run_file, "w", encoding="utf-8") as f:
                json.dump(self.last_run, f, indent=2, ensure_ascii=False)
        except IOError as e:
            logger.error(f"Error saving last run file: {e}")

    def is_video_processed(self, video_id: str) -> bool:
        """Check if a video has already been processed."""
        return video_id in self.state.get("processed_videos", {})

    def mark_video_processed(
        self,
        video_id: str,
        title: str,
        channel: str,
        summary_file: str,
        published_date: str = None
    ):
        """Mark a video as processed and store its metadata."""
        if "processed_videos" not in self.state:
            self.state["processed_videos"] = {}
        if "video_metadata" not in self.state:
            self.state["video_metadata"] = {}

        timestamp = datetime.now().isoformat()

        self.state["processed_videos"][video_id] = timestamp
        self.state["video_metadata"][video_id] = {
            "title": title,
            "channel": channel,
            "summary_file": summary_file,
            "published_date": published_date,
            "processed_at": timestamp
        }

        self._save_state()
        logger.info(f"Marked video as processed: {video_id} - {title}")

    def get_video_metadata(self, video_id: str) -> Optional[dict]:
        """Get metadata for a processed video."""
        return self.state.get("video_metadata", {}).get(video_id)

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

    def record_run_complete(self, videos_processed: int = 0, errors: int = 0):
        """Record the completion of a run."""
        self.last_run["last_run_time"] = datetime.now().isoformat()
        self.last_run["last_run_status"] = "success" if errors == 0 else "completed_with_errors"
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

    def get_all_processed_video_ids(self) -> set:
        """Get all processed video IDs."""
        return set(self.state.get("processed_videos", {}).keys())

    def cleanup_old_entries(self, days: int = 90):
        """Remove entries older than specified days to prevent state file bloat."""
        cutoff = datetime.now().timestamp() - (days * 24 * 60 * 60)

        to_remove = []
        for video_id, timestamp_str in self.state.get("processed_videos", {}).items():
            try:
                timestamp = datetime.fromisoformat(timestamp_str).timestamp()
                if timestamp < cutoff:
                    to_remove.append(video_id)
            except (ValueError, TypeError):
                continue

        for video_id in to_remove:
            self.state["processed_videos"].pop(video_id, None)
            self.state["video_metadata"].pop(video_id, None)

        if to_remove:
            self._save_state()
            logger.info(f"Cleaned up {len(to_remove)} old entries from state")

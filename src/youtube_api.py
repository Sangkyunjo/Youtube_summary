"""
YouTube Data API v3 client.
Shared wrapper used by channel_monitor and historical_fetcher.
"""

import re
import time
from typing import Optional

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from utils import load_config, get_env, setup_logging

logger = setup_logging("youtube_api")


def _sanitize_error(error: HttpError) -> str:
    """Remove API key from error details before logging."""
    return re.sub(r'key=[A-Za-z0-9_\-]+', 'key=REDACTED', str(error))


def _iso_seconds(value: str) -> int:
    """ISO 8601 duration such as PT1H2M3S → seconds (0 when absent)."""
    match = re.fullmatch(r'P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', value or '')
    if not match:
        return 0
    days, hours, minutes, seconds = (int(part or 0) for part in match.groups())
    return ((days * 24 + hours) * 60 + minutes) * 60 + seconds


class YouTubeAPI:
    """Thin wrapper around the YouTube Data API v3."""

    def __init__(self):
        api_key = get_env("YOUTUBE_API_KEY")
        if not api_key:
            raise ValueError(
                "YOUTUBE_API_KEY not set. "
                "Get one at https://console.cloud.google.com/ and add it to config/.env"
            )
        # Bundled discovery documents in older runtimes omit forHandle.
        self._service = build("youtube", "v3", developerKey=api_key, static_discovery=False)
        self._config = load_config()
        self._request_delay = self._config.get("youtube", {}).get("request_delay", 0.5)

    def _throttle(self):
        """Simple rate limiter between API calls."""
        time.sleep(self._request_delay)

    # ------------------------------------------------------------------
    # Channel resolution
    # ------------------------------------------------------------------

    def resolve_handle(self, handle: str) -> Optional[dict]:
        """
        Resolve a @handle to channel info.

        Returns:
            {"channel_id": "UCxxx", "title": "Channel Name"} or None
        """
        if not handle.startswith("@"):
            handle = f"@{handle}"

        try:
            self._throttle()
            resp = (
                self._service.channels()
                .list(part="snippet", forHandle=handle[1:])
                .execute()
            )
            items = resp.get("items", [])
            if items:
                item = items[0]
                return {
                    "channel_id": item["id"],
                    "title": item["snippet"]["title"],
                }
            logger.warning(f"No channel found for handle {handle}")
            return None
        except HttpError as e:
            logger.error(f"API error resolving handle {handle}: {_sanitize_error(e)}")
            return None

    def get_channel_info(self, channel_id: str) -> Optional[dict]:
        """Get channel title by ID."""
        try:
            self._throttle()
            resp = (
                self._service.channels()
                .list(part="snippet", id=channel_id)
                .execute()
            )
            items = resp.get("items", [])
            if items:
                return {
                    "channel_id": items[0]["id"],
                    "title": items[0]["snippet"]["title"],
                }
            return None
        except HttpError as e:
            logger.error(f"API error getting channel info {channel_id}: {_sanitize_error(e)}")
            return None

    # ------------------------------------------------------------------
    # Video listing via playlistItems (1 quota unit per page)
    # ------------------------------------------------------------------

    def list_playlist_videos(
        self,
        playlist_id: str,
        max_results: int = 50,
    ) -> list[dict]:
        """
        List videos from a playlist.

        Args:
            playlist_id: e.g. "UULF..." for long-form uploads
            max_results: maximum number of videos to return

        Returns:
            List of {"video_id", "title", "published_at"} dicts
        """
        videos = []
        page_token = None

        while len(videos) < max_results:
            try:
                self._throttle()
                per_page = min(50, max_results - len(videos))
                resp = (
                    self._service.playlistItems()
                    .list(
                        part="snippet",
                        playlistId=playlist_id,
                        maxResults=per_page,
                        pageToken=page_token,
                    )
                    .execute()
                )
            except HttpError as e:
                logger.error(f"API error listing playlist {playlist_id}: {_sanitize_error(e)}")
                break

            for item in resp.get("items", []):
                snippet = item["snippet"]
                vid = snippet["resourceId"].get("videoId")
                if not vid:
                    continue
                videos.append({
                    "video_id": vid,
                    "title": snippet["title"],
                    "published_at": snippet["publishedAt"],
                })

            page_token = resp.get("nextPageToken")
            if not page_token:
                break

        return videos

    # ------------------------------------------------------------------
    # Video details (1 quota unit per 50 IDs)
    # ------------------------------------------------------------------

    def get_video_durations(self, video_ids: list[str]) -> dict[str, dict]:
        """
        Look up length and live state for videos.

        Returns:
            {video_id: {"duration_seconds", "live"}}; deleted or private IDs are absent
        """
        found = {}
        for start in range(0, len(video_ids), 50):
            try:
                self._throttle()
                resp = (
                    self._service.videos()
                    .list(part="contentDetails,snippet", id=",".join(video_ids[start:start + 50]))
                    .execute()
                )
            except HttpError as e:
                logger.error(f"API error listing video details: {_sanitize_error(e)}")
                break

            for item in resp.get("items", []):
                found[item["id"]] = {
                    "duration_seconds": _iso_seconds(item["contentDetails"].get("duration", "")),
                    "live": item["snippet"].get("liveBroadcastContent", "none") != "none",
                }

        return found

    # ------------------------------------------------------------------
    # Search (100 quota units per call — use sparingly)
    # ------------------------------------------------------------------

    def search_channel_videos(
        self,
        channel_id: str,
        published_after: str,
        published_before: str,
        max_results: int = 50,
    ) -> list[dict]:
        """
        Search for videos on a channel within a date range.

        Args:
            channel_id: YouTube channel ID
            published_after: ISO 8601 datetime string (e.g. "2026-01-01T00:00:00Z")
            published_before: ISO 8601 datetime string
            max_results: maximum number of results

        Returns:
            List of {"video_id", "title", "published_at"} dicts
        """
        videos = []
        page_token = None

        while len(videos) < max_results:
            try:
                self._throttle()
                per_page = min(50, max_results - len(videos))
                resp = (
                    self._service.search()
                    .list(
                        part="snippet",
                        channelId=channel_id,
                        type="video",
                        order="date",
                        publishedAfter=published_after,
                        publishedBefore=published_before,
                        maxResults=per_page,
                        pageToken=page_token,
                    )
                    .execute()
                )
            except HttpError as e:
                logger.error(f"API error searching channel {channel_id}: {_sanitize_error(e)}")
                break

            for item in resp.get("items", []):
                vid = item["id"].get("videoId")
                if not vid:
                    continue
                videos.append({
                    "video_id": vid,
                    "title": item["snippet"]["title"],
                    "published_at": item["snippet"]["publishedAt"],
                })

            page_token = resp.get("nextPageToken")
            if not page_token:
                break

        return videos

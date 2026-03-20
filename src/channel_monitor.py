"""
YouTube Channel Monitoring module.
Uses the YouTube Data API v3 to list recent uploads.
Falls back to RSS feeds if the API is unavailable.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional
from dataclasses import dataclass

from dateutil import parser as date_parser

from utils import load_config, load_channels, setup_logging
from youtube_api import YouTubeAPI

logger = setup_logging("channel_monitor")


@dataclass
class VideoInfo:
    """Information about a YouTube video."""
    video_id: str
    title: str
    channel_name: str
    channel_id: str
    published_date: datetime
    url: str
    channel_handle: Optional[str] = None


class ChannelMonitor:
    """Monitors YouTube channels for new videos using the YouTube Data API."""

    def __init__(self):
        self.config = load_config()
        self.max_video_age_days = self.config["schedule"]["max_video_age_days"]
        self._channel_id_cache: dict[str, dict] = {}
        self._api = YouTubeAPI()

    @property
    def api(self) -> YouTubeAPI:
        """Public accessor for the YouTube API client."""
        return self._api

    # ------------------------------------------------------------------
    # Channel resolution
    # ------------------------------------------------------------------

    def resolve_handle_to_channel_id(self, handle: str) -> Optional[str]:
        """
        Resolve a YouTube @handle to a channel ID via the Data API.

        Returns:
            Channel ID (e.g., "UCxxxxxxxxxxxxxxxxxxxxxx") or None
        """
        if not handle.startswith("@"):
            handle = f"@{handle}"

        if handle in self._channel_id_cache:
            return self._channel_id_cache[handle]["channel_id"]

        info = self._api.resolve_handle(handle)
        if info:
            self._channel_id_cache[handle] = info
            logger.info(f"Resolved {handle} -> {info['channel_id']} ({info['title']})")
            return info["channel_id"]

        logger.error(f"Failed to resolve handle {handle}")
        return None

    def get_channel_id(self, channel_input: str) -> Optional[str]:
        """Get a channel ID from either a handle or direct ID."""
        if channel_input.startswith("UC") and len(channel_input) == 24:
            return channel_input
        return self.resolve_handle_to_channel_id(channel_input)

    def get_channel_name(self, channel_input: str) -> Optional[str]:
        """Get the channel display name."""
        # For raw channel IDs, query the API
        if channel_input.startswith("UC") and len(channel_input) == 24:
            info = self._api.get_channel_info(channel_input)
            return info["title"] if info else None

        # Normalize handle for cache lookup
        handle_key = channel_input if channel_input.startswith("@") else f"@{channel_input}"

        if handle_key in self._channel_id_cache:
            return self._channel_id_cache[handle_key].get("title")

        # Resolve handle (populates cache)
        self.resolve_handle_to_channel_id(channel_input)
        if handle_key in self._channel_id_cache:
            return self._channel_id_cache[handle_key].get("title")
        return None

    # ------------------------------------------------------------------
    # Uploads playlist ID
    # ------------------------------------------------------------------

    @staticmethod
    def _uploads_playlist_id(channel_id: str, long_form_only: bool = True) -> str:
        """
        Build the uploads playlist ID for a channel.

        UULF prefix = long-form videos only (default)
        UU   prefix = all uploads
        """
        suffix = channel_id[2:]  # strip "UC"
        prefix = "UULF" if long_form_only else "UU"
        return f"{prefix}{suffix}"

    # ------------------------------------------------------------------
    # Fetch videos
    # ------------------------------------------------------------------

    def fetch_feed(self, channel_input: str) -> list[VideoInfo]:
        """
        Fetch recent uploads for a channel via the Data API.

        Args:
            channel_input: Channel handle or ID

        Returns:
            List of VideoInfo objects for recent videos
        """
        channel_id = self.get_channel_id(channel_input)
        if not channel_id:
            logger.error(f"Could not get channel ID for: {channel_input}")
            return []

        # Resolve channel name
        channel_name = self.get_channel_name(channel_input) or channel_input

        playlist_id = self._uploads_playlist_id(channel_id)
        logger.debug(f"Fetching playlist {playlist_id} for {channel_name}")

        raw_videos = self._api.list_playlist_videos(
            playlist_id=playlist_id,
            max_results=50,
        )

        cutoff_date = datetime.now(timezone.utc) - timedelta(days=self.max_video_age_days)
        videos: list[VideoInfo] = []

        for rv in raw_videos:
            try:
                published_date = date_parser.parse(rv["published_at"])
                # Ensure timezone-aware for correct comparison
                if published_date.tzinfo is None:
                    published_date = published_date.replace(tzinfo=timezone.utc)

                if published_date < cutoff_date:
                    continue

                # Store as naive datetime for downstream compatibility
                naive_date = published_date.replace(tzinfo=None)
                videos.append(VideoInfo(
                    video_id=rv["video_id"],
                    title=rv["title"],
                    channel_name=channel_name,
                    channel_id=channel_id,
                    published_date=naive_date,
                    url=f"https://www.youtube.com/watch?v={rv['video_id']}",
                    channel_handle=channel_input,
                ))
            except (KeyError, ValueError) as e:
                logger.warning(f"Error parsing video entry: {e}")
                continue

        logger.info(f"Found {len(videos)} recent videos for {channel_name}")
        return videos

    def get_all_new_videos(self, processed_ids: Optional[set] = None) -> list[VideoInfo]:
        """
        Get all new videos from all configured channels.

        Args:
            processed_ids: Set of already-processed video IDs

        Returns:
            List of VideoInfo objects for new videos
        """
        if processed_ids is None:
            processed_ids = set()

        channels = load_channels()
        if not channels:
            logger.warning("No channels configured in channels.yaml")
            return []

        all_videos: list[VideoInfo] = []

        for channel in channels:
            if not channel:
                continue

            videos = self.fetch_feed(channel)

            for video in videos:
                if video.video_id not in processed_ids:
                    all_videos.append(video)
                else:
                    logger.debug(f"Skipping already processed: {video.title}")

        all_videos.sort(key=lambda v: v.published_date, reverse=True)
        logger.info(f"Found {len(all_videos)} new videos total")
        return all_videos


def main():
    """Test the channel monitor."""
    monitor = ChannelMonitor()

    channels = load_channels()
    if channels:
        for channel in channels:
            print(f"\nTesting channel: {channel}")
            videos = monitor.fetch_feed(channel)
            for video in videos[:3]:
                print(f"  - {video.title} ({video.published_date})")
    else:
        print("No channels configured. Add channels to config/channels.yaml")


if __name__ == "__main__":
    main()

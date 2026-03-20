"""
Historical video fetcher module.
Uses the YouTube Data API v3 to fetch videos within a date range.
"""

from datetime import datetime

from dateutil import parser as date_parser

from utils import setup_logging

logger = setup_logging("historical_fetcher")


class HistoricalFetcher:
    """Fetches historical videos from a YouTube channel via the Data API."""

    def __init__(self, channel_monitor):
        """
        Args:
            channel_monitor: ChannelMonitor instance for resolving handles
        """
        self.channel_monitor = channel_monitor
        self._api = channel_monitor.api  # reuse the same API client

    def fetch_videos(
        self,
        channel_input: str,
        start_date: datetime,
        end_date: datetime,
    ) -> list:
        """
        Fetch all videos from a channel within a date range.

        Uses the playlistItems endpoint (1 quota unit per page) and filters
        by date client-side, which is far cheaper than the search endpoint
        (100 units per call).

        Args:
            channel_input: Channel handle (@kpunch) or ID (UCxxxxxx)
            start_date: Start date (inclusive)
            end_date: End date (inclusive)

        Returns:
            List of VideoInfo objects
        """
        from channel_monitor import ChannelMonitor, VideoInfo

        channel_id = self.channel_monitor.get_channel_id(channel_input)
        if not channel_id:
            logger.error(f"Could not resolve channel: {channel_input}")
            return []

        channel_name = self.channel_monitor.get_channel_name(channel_input) or channel_input

        logger.info(f"Fetching videos from {channel_name} ({channel_id})")
        logger.info(
            f"Date range: {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}"
        )

        playlist_id = ChannelMonitor._uploads_playlist_id(channel_id)

        raw_videos = self._api.list_playlist_videos(
            playlist_id=playlist_id,
            max_results=500,  # allow more pages for historical
        )

        if len(raw_videos) >= 500:
            logger.warning(
                f"Reached max_results limit (500) for {channel_name}. "
                f"Videos older than the 500th upload may be missing from results."
            )

        videos = []
        for rv in raw_videos:
            try:
                published = date_parser.parse(rv["published_at"])
                if published.tzinfo is not None:
                    published = published.replace(tzinfo=None)

                if published.date() < start_date.date():
                    continue
                if published.date() > end_date.date():
                    continue

                videos.append(VideoInfo(
                    video_id=rv["video_id"],
                    title=rv["title"],
                    channel_name=channel_name,
                    channel_id=channel_id,
                    published_date=published,
                    url=f"https://www.youtube.com/watch?v={rv['video_id']}",
                    channel_handle=channel_input,
                ))
            except (KeyError, ValueError) as e:
                logger.warning(f"Failed to parse video entry: {e}")
                continue

        logger.info(f"Found {len(videos)} videos in date range")
        return videos

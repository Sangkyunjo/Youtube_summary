"""
YouTube Channel Monitoring module.
Uses RSS feeds with UULF prefix for long-form videos only.
Resolves @handle URLs to channel IDs using yt-dlp.
"""

import re
import subprocess
import sys
import json
from datetime import datetime, timedelta
from typing import Optional
from dataclasses import dataclass

import feedparser
from dateutil import parser as date_parser

from utils import load_config, load_channels, setup_logging

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
    """Monitors YouTube channels for new videos using RSS feeds."""

    # RSS feed URL format with UULF prefix for long-form videos
    RSS_URL_TEMPLATE = "https://www.youtube.com/feeds/videos.xml?playlist_id=UULF{channel_id_suffix}"

    def __init__(self):
        self.config = load_config()
        self.max_video_age_days = self.config["schedule"]["max_video_age_days"]
        self._channel_id_cache = {}

    def resolve_handle_to_channel_id(self, handle: str) -> Optional[str]:
        """
        Resolve a YouTube @handle to a channel ID using yt-dlp.

        Args:
            handle: YouTube handle (e.g., "@kpunch" or "kpunch")

        Returns:
            Channel ID (e.g., "UCxxxxxxxxxxxxxxxxxxxxxx") or None if failed
        """
        # Normalize handle
        if not handle.startswith("@"):
            handle = f"@{handle}"

        # Check cache
        if handle in self._channel_id_cache:
            return self._channel_id_cache[handle]

        url = f"https://www.youtube.com/{handle}"
        logger.info(f"Resolving handle {handle} to channel ID...")

        try:
            result = subprocess.run(
                [
                    sys.executable, "-m", "yt_dlp",
                    "--print", "channel_id",
                    "--playlist-items", "1",
                    "--no-warnings",
                    "--quiet",
                    url
                ],
                capture_output=True,
                text=True,
                timeout=60
            )

            if result.returncode == 0 and result.stdout.strip():
                channel_id = result.stdout.strip().split('\n')[0]
                if channel_id.startswith("UC") and len(channel_id) == 24:
                    self._channel_id_cache[handle] = channel_id
                    logger.info(f"Resolved {handle} to {channel_id}")
                    return channel_id

            logger.error(f"Failed to resolve handle {handle}: {result.stderr}")
            return None

        except subprocess.TimeoutExpired:
            logger.error(f"Timeout resolving handle {handle}")
            return None
        except FileNotFoundError:
            logger.error("Python or yt-dlp not found. Please install yt-dlp: pip install yt-dlp")
            return None
        except Exception as e:
            logger.error(f"Error resolving handle {handle}: {e}")
            return None

    def get_channel_id(self, channel_input: str) -> Optional[str]:
        """
        Get a channel ID from either a handle or direct ID.

        Args:
            channel_input: Either "@handle" or "UCxxxxxx" channel ID

        Returns:
            Channel ID or None if failed
        """
        # If it's already a channel ID
        if channel_input.startswith("UC") and len(channel_input) == 24:
            return channel_input

        # If it's a handle, resolve it
        return self.resolve_handle_to_channel_id(channel_input)

    def get_channel_name(self, channel_id: str) -> Optional[str]:
        """
        Get the channel name for a channel ID using yt-dlp.

        Args:
            channel_id: YouTube channel ID (UCxxxxxx)

        Returns:
            Channel name or None if failed
        """
        channel_url = f"https://www.youtube.com/channel/{channel_id}"

        try:
            result = subprocess.run(
                [
                    sys.executable, "-m", "yt_dlp",
                    "--print", "channel",
                    "--playlist-items", "1",
                    "--no-warnings",
                    "--quiet",
                    channel_url
                ],
                capture_output=True,
                text=True,
                timeout=60
            )

            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip().split('\n')[0]

            return None

        except Exception as e:
            logger.warning(f"Could not get channel name: {e}")
            return None

    def get_rss_feed_url(self, channel_id: str) -> str:
        """
        Generate the RSS feed URL for long-form videos.
        Uses UULF prefix to filter for long-form content only.

        Args:
            channel_id: Full channel ID (UCxxxxxx)

        Returns:
            RSS feed URL
        """
        # Remove "UC" prefix to get the suffix
        channel_id_suffix = channel_id[2:]
        return self.RSS_URL_TEMPLATE.format(channel_id_suffix=channel_id_suffix)

    def fetch_feed(self, channel_input: str) -> list[VideoInfo]:
        """
        Fetch and parse the RSS feed for a channel.

        Args:
            channel_input: Channel handle or ID

        Returns:
            List of VideoInfo objects for recent videos
        """
        channel_id = self.get_channel_id(channel_input)
        if not channel_id:
            logger.error(f"Could not get channel ID for: {channel_input}")
            return []

        feed_url = self.get_rss_feed_url(channel_id)
        logger.debug(f"Fetching feed: {feed_url}")

        try:
            feed = feedparser.parse(feed_url)

            if feed.bozo:
                logger.warning(f"Feed parsing issue for {channel_input}: {feed.bozo_exception}")

            if not feed.entries:
                logger.info(f"No entries found in feed for {channel_input}")
                return []

            channel_name = feed.feed.get("title", channel_input)
            # Clean up channel name (remove " - Videos" suffix if present)
            channel_name = re.sub(r'\s*-\s*Videos$', '', channel_name)

            videos = []
            cutoff_date = datetime.now() - timedelta(days=self.max_video_age_days)

            for entry in feed.entries:
                try:
                    video_id = entry.yt_videoid
                    title = entry.title
                    published_str = entry.published
                    published_date = date_parser.parse(published_str)

                    # Make datetime timezone-naive for comparison
                    if published_date.tzinfo is not None:
                        published_date = published_date.replace(tzinfo=None)

                    # Skip videos older than cutoff
                    if published_date < cutoff_date:
                        continue

                    video_url = f"https://www.youtube.com/watch?v={video_id}"

                    videos.append(VideoInfo(
                        video_id=video_id,
                        title=title,
                        channel_name=channel_name,
                        channel_id=channel_id,
                        published_date=published_date,
                        url=video_url,
                        channel_handle=channel_input
                    ))

                except (AttributeError, KeyError) as e:
                    logger.warning(f"Error parsing entry: {e}")
                    continue

            logger.info(f"Found {len(videos)} recent videos for {channel_name}")
            return videos

        except Exception as e:
            logger.error(f"Error fetching feed for {channel_input}: {e}")
            return []

    def get_all_new_videos(self, processed_ids: set = None) -> list[VideoInfo]:
        """
        Get all new videos from all configured channels.

        Args:
            processed_ids: Set of video IDs that have already been processed

        Returns:
            List of VideoInfo objects for new videos
        """
        if processed_ids is None:
            processed_ids = set()

        channels = load_channels()
        if not channels:
            logger.warning("No channels configured in channels.yaml")
            return []

        all_videos = []

        for channel in channels:
            if not channel:
                continue

            videos = self.fetch_feed(channel)

            for video in videos:
                if video.video_id not in processed_ids:
                    all_videos.append(video)
                else:
                    logger.debug(f"Skipping already processed: {video.title}")

        # Sort by published date (newest first)
        all_videos.sort(key=lambda v: v.published_date, reverse=True)

        logger.info(f"Found {len(all_videos)} new videos total")
        return all_videos


def main():
    """Test the channel monitor."""
    monitor = ChannelMonitor()

    # Test with a sample channel
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

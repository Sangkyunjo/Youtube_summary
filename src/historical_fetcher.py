"""
Historical video fetcher module.
Uses yt-dlp to fetch all videos from a channel and filter by date range.
"""

import subprocess
import sys
from datetime import datetime
from typing import Optional

from utils import setup_logging

logger = setup_logging("historical_fetcher")


class HistoricalFetcher:
    """Fetches historical videos from a YouTube channel using yt-dlp."""

    def __init__(self, channel_monitor):
        """
        Initialize the historical fetcher.

        Args:
            channel_monitor: ChannelMonitor instance for resolving handles
        """
        self.channel_monitor = channel_monitor

    def fetch_videos(
        self,
        channel_input: str,
        start_date: datetime,
        end_date: datetime
    ) -> list:
        """
        Fetch all videos from a channel within a date range.

        Args:
            channel_input: Channel handle (@kpunch) or ID (UCxxxxxx)
            start_date: Start date (inclusive)
            end_date: End date (inclusive)

        Returns:
            List of VideoInfo objects
        """
        from channel_monitor import VideoInfo

        # Resolve channel ID
        channel_id = self.channel_monitor.get_channel_id(channel_input)
        if not channel_id:
            logger.error(f"Could not resolve channel: {channel_input}")
            return []

        # Get channel name
        channel_name = self.get_channel_name(channel_id)
        if not channel_name:
            channel_name = channel_input

        logger.info(f"Fetching videos from {channel_name} ({channel_id})")
        logger.info(f"Date range: {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}")

        # Use yt-dlp with --print to get video info including dates
        # --dateafter and --datebefore filter by upload date
        channel_url = f"https://www.youtube.com/channel/{channel_id}/videos"

        # Format dates for yt-dlp (YYYYMMDD)
        date_after = start_date.strftime('%Y%m%d')
        date_before = end_date.strftime('%Y%m%d')

        try:
            # Use --print to get video_id, title, and upload_date in a parseable format
            # The separator "|DELIM|" is chosen to avoid conflicts with video titles
            result = subprocess.run(
                [
                    sys.executable, "-m", "yt_dlp",
                    "--print", "%(id)s|DELIM|%(title)s|DELIM|%(upload_date)s",
                    "--no-warnings",
                    "--dateafter", date_after,
                    "--datebefore", date_before,
                    channel_url
                ],
                capture_output=True,
                text=True,
                timeout=600  # 10 minute timeout for date-filtered extraction
            )

            if result.returncode != 0 and not result.stdout.strip():
                logger.error(f"yt-dlp failed: {result.stderr}")
                return []

            # Parse output lines
            videos = []
            for line in result.stdout.strip().split('\n'):
                if not line or '|DELIM|' not in line:
                    continue

                try:
                    parts = line.split('|DELIM|')
                    if len(parts) != 3:
                        continue

                    video_id, title, upload_date_str = parts

                    # Parse upload date (format: YYYYMMDD)
                    try:
                        upload_date = datetime.strptime(upload_date_str, '%Y%m%d')
                    except ValueError:
                        logger.warning(f"Invalid date format for {video_id}: {upload_date_str}")
                        continue

                    video_url = f"https://www.youtube.com/watch?v={video_id}"

                    videos.append(VideoInfo(
                        video_id=video_id,
                        title=title,
                        channel_name=channel_name,
                        channel_id=channel_id,
                        published_date=upload_date,
                        url=video_url
                    ))
                except Exception as e:
                    logger.warning(f"Failed to parse video line: {e}")
                    continue

            logger.info(f"Found {len(videos)} videos in date range")
            return videos

        except subprocess.TimeoutExpired:
            logger.error("Timeout fetching video list from channel")
            return []
        except FileNotFoundError:
            logger.error("yt-dlp not found. Please install: pip install yt-dlp")
            return []
        except Exception as e:
            logger.error(f"Error fetching videos: {e}")
            return []

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

"""
Tests for the historical fetcher with @wsaj channel.
"""

import sys
from pathlib import Path
from datetime import datetime

# Add src directory to path
sys.path.insert(0, str(Path(__file__).parent / "src"))

from channel_monitor import ChannelMonitor
from historical_fetcher import HistoricalFetcher
from state_manager import StateManager


def test_resolve_channel():
    """Test that @wsaj channel can be resolved."""
    print("=" * 60)
    print("Test 1: Resolve @wsaj channel")
    print("=" * 60)

    monitor = ChannelMonitor()
    channel_id = monitor.get_channel_id("@wsaj")

    if channel_id:
        print(f"[OK] Resolved @wsaj to: {channel_id}")

        # Get channel name
        channel_name = monitor.get_channel_name(channel_id)
        print(f"[OK] Channel name: {channel_name}")
        return channel_id
    else:
        print("[FAIL] Failed to resolve @wsaj")
        return None


def test_fetch_all_videos(channel_id: str):
    """Fetch all videos from @wsaj channel."""
    print("\n" + "=" * 60)
    print("Test 2: Fetch all videos from @wsaj")
    print("=" * 60)

    monitor = ChannelMonitor()
    fetcher = HistoricalFetcher(monitor)

    # Fetch videos from this year (2026)
    start_date = datetime(2026, 1, 1)
    end_date = datetime.now()

    print(f"Fetching videos from {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}")
    print("This may take a while...")

    videos = fetcher.fetch_videos("@wsaj", start_date, end_date)

    print(f"\n[OK] Found {len(videos)} total videos")

    if videos:
        # Sort by date (oldest first)
        videos.sort(key=lambda v: v.published_date)

        print(f"\nOldest video: {videos[0].published_date.strftime('%Y-%m-%d')} - {videos[0].title[:50]}...")
        print(f"Newest video: {videos[-1].published_date.strftime('%Y-%m-%d')} - {videos[-1].title[:50]}...")

    return videos


def test_check_processed_status(videos: list):
    """Check how many videos are already processed."""
    print("\n" + "=" * 60)
    print("Test 3: Check processed status")
    print("=" * 60)

    state = StateManager()
    processed_ids = state.get_all_processed_video_ids()

    already_processed = []
    not_processed = []

    for video in videos:
        if video.video_id in processed_ids:
            already_processed.append(video)
        else:
            not_processed.append(video)

    print(f"Total videos: {len(videos)}")
    print(f"Already processed: {len(already_processed)}")
    print(f"Not yet processed: {len(not_processed)}")

    if not_processed:
        print(f"\nVideos to process:")
        for video in not_processed[:10]:  # Show first 10
            print(f"  - {video.published_date.strftime('%Y-%m-%d')} | {video.title[:60]}")
        if len(not_processed) > 10:
            print(f"  ... and {len(not_processed) - 10} more")

    return not_processed


def main():
    """Run all tests."""
    print("\n" + "=" * 60)
    print("Historical Fetcher Tests for @wsaj")
    print("=" * 60 + "\n")

    # Test 1: Resolve channel
    channel_id = test_resolve_channel()
    if not channel_id:
        print("\nAborting: Could not resolve channel")
        return

    # Test 2: Fetch all videos
    videos = test_fetch_all_videos(channel_id)
    if not videos:
        print("\nNo videos found")
        return

    # Test 3: Check processed status
    to_process = test_check_processed_status(videos)

    # Summary
    print("\n" + "=" * 60)
    print("Summary")
    print("=" * 60)
    print(f"\nTo process all {len(to_process)} unprocessed videos from @wsaj, run:")

    if videos:
        oldest = min(v.published_date for v in videos)
        newest = max(v.published_date for v in videos)
        print(f"\npython src/main.py --historical --channel @wsaj \\")
        print(f"    --start-date {oldest.strftime('%Y-%m-%d')} \\")
        print(f"    --end-date {newest.strftime('%Y-%m-%d')} \\")
        print(f"    --skip-email")

        print(f"\nOr for a dry run first:")
        print(f"\npython src/main.py --historical --channel @wsaj \\")
        print(f"    --start-date {oldest.strftime('%Y-%m-%d')} \\")
        print(f"    --end-date {newest.strftime('%Y-%m-%d')} \\")
        print(f"    --dry-run")


if __name__ == "__main__":
    main()

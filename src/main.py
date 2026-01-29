"""
Main entry point for the YouTube Summary System.
Orchestrates all components: monitoring, extraction, summarization, and notification.
"""

import sys
import argparse
from datetime import datetime
from pathlib import Path

# Add src directory to path for imports
sys.path.insert(0, str(Path(__file__).parent))

from utils import (
    setup_logging,
    load_config,
    load_channels,
    generate_summary_filename,
    get_summaries_dir
)
from state_manager import StateManager
from channel_monitor import ChannelMonitor, VideoInfo
from historical_fetcher import HistoricalFetcher
from transcript_extractor import TranscriptExtractor
from summarizer import Summarizer
from email_notifier import EmailNotifier
from scheduler import Scheduler

logger = setup_logging("main")


class YouTubeSummarySystem:
    """Main orchestrator for the YouTube Summary System."""

    def __init__(self):
        self.config = load_config()
        self.state_manager = StateManager()
        self.channel_monitor = ChannelMonitor()
        self.transcript_extractor = TranscriptExtractor()
        self.summarizer = None  # Initialized on demand
        self.email_notifier = EmailNotifier()
        self.scheduler = Scheduler(self.state_manager)

    def _ensure_summarizer(self):
        """Initialize summarizer on demand."""
        if self.summarizer is None:
            try:
                self.summarizer = Summarizer()
            except ValueError as e:
                logger.error(f"Failed to initialize summarizer: {e}")
                raise

    def process_video(self, video: VideoInfo) -> dict:
        """
        Process a single video: extract transcript, summarize, save.

        Args:
            video: VideoInfo object with video details

        Returns:
            Dictionary with processing result
        """
        result = {
            "video_id": video.video_id,
            "title": video.title,
            "channel": video.channel_name,
            "success": False,
            "error": None,
            "summary_file": None
        }

        logger.info(f"Processing: {video.title} ({video.video_id})")

        # Extract transcript
        transcript = self.transcript_extractor.extract(video.video_id)
        if not transcript:
            result["error"] = "No transcript available"
            logger.warning(f"No transcript for: {video.title}")
            return result

        # Generate summary
        self._ensure_summarizer()
        sections = self.summarizer.summarize(
            transcript=transcript,
            title=video.title,
            channel=video.channel_name
        )

        if not sections:
            result["error"] = "Summarization failed"
            logger.error(f"Summarization failed for: {video.title}")
            return result

        # Create formatted summary
        summary_text = self.summarizer.create_summary_text(
            title=video.title,
            channel=video.channel_name,
            published=video.published_date.strftime("%Y-%m-%d %H:%M"),
            url=video.url,
            sections=sections
        )

        # Save summary file
        filename = generate_summary_filename(
            channel_name=video.channel_name,
            video_title=video.title,
            published_date=video.published_date
        )
        summary_path = get_summaries_dir() / filename

        try:
            with open(summary_path, "w", encoding="utf-8") as f:
                f.write(summary_text)

            result["success"] = True
            result["summary_file"] = filename
            logger.info(f"Summary saved: {filename}")

            # Mark as processed
            self.state_manager.mark_video_processed(
                video_id=video.video_id,
                title=video.title,
                channel=video.channel_name,
                summary_file=filename,
                published_date=video.published_date.strftime("%Y-%m-%d")
            )

        except IOError as e:
            result["error"] = f"Failed to save summary: {e}"
            logger.error(f"Failed to save summary for {video.title}: {e}")

        return result

    def run(self, force: bool = False, skip_email: bool = False) -> dict:
        """
        Run the main processing pipeline.

        Args:
            force: If True, run even if already ran today
            skip_email: If True, skip sending email notification

        Returns:
            Dictionary with run results
        """
        run_result = {
            "start_time": datetime.now().isoformat(),
            "videos_found": 0,
            "videos_processed": 0,
            "videos_skipped": 0,
            "errors": 0,
            "processed_videos": [],
            "email_sent": False
        }

        # Check if should run
        if not force and not self.scheduler.log_run_decision():
            run_result["skipped"] = True
            run_result["skip_reason"] = "Already ran today"
            return run_result

        self.state_manager.record_run_start()

        try:
            # Check for channels
            channels = load_channels()
            if not channels:
                logger.warning("No channels configured in channels.yaml")
                run_result["error"] = "No channels configured"
                self.state_manager.record_run_complete(0, 0)
                return run_result

            # Get new videos
            processed_ids = self.state_manager.get_all_processed_video_ids()
            new_videos = self.channel_monitor.get_all_new_videos(processed_ids)

            run_result["videos_found"] = len(new_videos)
            logger.info(f"Found {len(new_videos)} new videos to process")

            if not new_videos:
                logger.info("No new videos to process")
                self.state_manager.record_run_complete(0, 0)
                return run_result

            # Process each video
            for video in new_videos:
                result = self.process_video(video)

                if result["success"]:
                    run_result["videos_processed"] += 1
                    run_result["processed_videos"].append({
                        "video_id": result["video_id"],
                        "title": result["title"],
                        "channel": result["channel"],
                        "summary_file": result["summary_file"],
                        "published_date": video.published_date.strftime("%Y-%m-%d")
                    })
                elif result["error"] == "No transcript available":
                    run_result["videos_skipped"] += 1
                else:
                    run_result["errors"] += 1

            # Send email notification
            if not skip_email and run_result["processed_videos"]:
                email_sent = self.email_notifier.send_daily_report(
                    run_result["processed_videos"]
                )
                run_result["email_sent"] = email_sent

            # Record completion
            self.state_manager.record_run_complete(
                videos_processed=run_result["videos_processed"],
                errors=run_result["errors"]
            )

            # Cleanup old state entries periodically
            self.state_manager.cleanup_old_entries(days=90)

        except Exception as e:
            logger.exception(f"Run failed with error: {e}")
            self.state_manager.record_run_failed(str(e))
            run_result["error"] = str(e)

            # Try to send error notification
            if not skip_email:
                self.email_notifier.send_error_notification(str(e))

        run_result["end_time"] = datetime.now().isoformat()
        return run_result

    def run_historical(
        self,
        channel: str,
        start_date: datetime,
        end_date: datetime,
        reprocess: bool = False,
        skip_email: bool = False,
        dry_run: bool = False
    ) -> dict:
        """
        Process historical videos from a specific channel within a date range.

        Args:
            channel: Channel handle (@kpunch) or ID
            start_date: Start date (inclusive)
            end_date: End date (inclusive)
            reprocess: If True, reprocess already-processed videos
            skip_email: If True, skip sending email notification
            dry_run: If True, only show what would be processed

        Returns:
            Dictionary with run results
        """
        run_result = {
            "mode": "historical",
            "channel": channel,
            "start_date": start_date.strftime("%Y-%m-%d"),
            "end_date": end_date.strftime("%Y-%m-%d"),
            "start_time": datetime.now().isoformat(),
            "videos_found": 0,
            "videos_processed": 0,
            "videos_skipped": 0,
            "errors": 0,
            "processed_videos": [],
            "email_sent": False
        }

        # Fetch historical videos
        fetcher = HistoricalFetcher(self.channel_monitor)
        videos = fetcher.fetch_videos(channel, start_date, end_date)

        run_result["videos_found"] = len(videos)
        logger.info(f"Found {len(videos)} videos in date range")

        if not videos:
            logger.info("No videos found in date range")
            return run_result

        # Dry run mode - just show what would be processed
        if dry_run:
            print(f"\nDry Run - Found {len(videos)} videos in date range:")
            print("-" * 60)
            for video in videos:
                already_processed = self.state_manager.is_video_processed(video.video_id)
                status = "[SKIP - already processed]" if already_processed and not reprocess else "[WOULD PROCESS]"
                print(f"  {status} {video.published_date.strftime('%Y-%m-%d')} - {video.title}")
            return run_result

        # Process each video
        for video in videos:
            # Check if already processed (unless reprocess flag is set)
            if not reprocess and self.state_manager.is_video_processed(video.video_id):
                logger.info(f"Skipping already processed: {video.title}")
                run_result["videos_skipped"] += 1
                continue

            result = self.process_video(video)

            if result["success"]:
                run_result["videos_processed"] += 1
                run_result["processed_videos"].append({
                    "video_id": result["video_id"],
                    "title": result["title"],
                    "channel": result["channel"],
                    "summary_file": result["summary_file"],
                    "published_date": video.published_date.strftime("%Y-%m-%d")
                })
            elif result["error"] == "No transcript available":
                run_result["videos_skipped"] += 1
            else:
                run_result["errors"] += 1

        # Send email notification
        if not skip_email and run_result["processed_videos"]:
            email_sent = self.email_notifier.send_daily_report(
                run_result["processed_videos"]
            )
            run_result["email_sent"] = email_sent

        run_result["end_time"] = datetime.now().isoformat()
        return run_result

    def status(self) -> dict:
        """Get the current system status."""
        return {
            "schedule": self.scheduler.get_schedule_status(),
            "channels_configured": len(load_channels()),
            "processed_videos_count": len(self.state_manager.get_all_processed_video_ids()),
            "email_enabled": self.email_notifier.enabled
        }


def main():
    """Main entry point with CLI argument parsing."""
    parser = argparse.ArgumentParser(
        description="YouTube Channel Monitoring and Summarization System"
    )
    parser.add_argument(
        "--force", "-f",
        action="store_true",
        help="Force run even if already ran today"
    )
    parser.add_argument(
        "--skip-email", "-s",
        action="store_true",
        help="Skip sending email notification"
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Show system status and exit"
    )
    parser.add_argument(
        "--dry-run", "-n",
        action="store_true",
        help="Show what would be done without processing"
    )

    # Historical mode arguments
    parser.add_argument(
        "--historical",
        action="store_true",
        help="Enable historical processing mode"
    )
    parser.add_argument(
        "--channel",
        type=str,
        help="Channel handle (@kpunch) or ID for historical mode"
    )
    parser.add_argument(
        "--start-date",
        type=str,
        help="Start date (YYYY-MM-DD) for historical mode"
    )
    parser.add_argument(
        "--end-date",
        type=str,
        help="End date (YYYY-MM-DD) for historical mode"
    )
    parser.add_argument(
        "--reprocess",
        action="store_true",
        help="Reprocess already-processed videos (historical mode only)"
    )

    args = parser.parse_args()

    # Handle historical mode
    if args.historical:
        # Validate required arguments
        if not args.channel:
            print("Error: --channel is required for historical mode")
            sys.exit(1)
        if not args.start_date:
            print("Error: --start-date is required for historical mode")
            sys.exit(1)
        if not args.end_date:
            print("Error: --end-date is required for historical mode")
            sys.exit(1)

        # Parse dates
        try:
            start_date = datetime.strptime(args.start_date, "%Y-%m-%d")
            end_date = datetime.strptime(args.end_date, "%Y-%m-%d")
        except ValueError as e:
            print(f"Error: Invalid date format. Use YYYY-MM-DD. ({e})")
            sys.exit(1)

        if start_date > end_date:
            print("Error: start-date must be before or equal to end-date")
            sys.exit(1)

        system = YouTubeSummarySystem()

        print(f"\nHistorical Video Processing")
        print("=" * 50)
        print(f"Channel: {args.channel}")
        print(f"Date range: {args.start_date} to {args.end_date}")
        print(f"Reprocess: {args.reprocess}")
        print(f"Dry run: {args.dry_run}")
        print()

        result = system.run_historical(
            channel=args.channel,
            start_date=start_date,
            end_date=end_date,
            reprocess=args.reprocess,
            skip_email=args.skip_email,
            dry_run=args.dry_run
        )

        if not args.dry_run:
            print(f"\nRun Complete")
            print("-" * 30)
            print(f"Videos found: {result['videos_found']}")
            print(f"Videos processed: {result['videos_processed']}")
            print(f"Videos skipped: {result['videos_skipped']}")
            print(f"Errors: {result['errors']}")
            print(f"Email sent: {result['email_sent']}")

        return

    system = YouTubeSummarySystem()

    if args.status:
        print("\nYouTube Summary System Status")
        print("=" * 50)
        status = system.status()

        print(f"\nChannels configured: {status['channels_configured']}")
        print(f"Videos processed (total): {status['processed_videos_count']}")
        print(f"Email notifications: {'Enabled' if status['email_enabled'] else 'Disabled'}")

        sched = status['schedule']
        print(f"\nLast run: {sched['last_run_time'] or 'Never'}")
        print(f"Last status: {sched['last_run_status'] or 'N/A'}")
        print(f"Should run now: {sched['should_run_now']}")
        print(f"Reason: {sched['run_reason']}")
        return

    if args.dry_run:
        print("\nDry Run - Checking for new videos...")
        print("=" * 50)

        channels = load_channels()
        print(f"Channels configured: {len(channels)}")

        if channels:
            monitor = ChannelMonitor()
            state = StateManager()
            processed_ids = state.get_all_processed_video_ids()
            new_videos = monitor.get_all_new_videos(processed_ids)

            print(f"New videos found: {len(new_videos)}")
            for video in new_videos:
                print(f"  - [{video.channel_name}] {video.title}")
        return

    # Run the system
    print("\nStarting YouTube Summary System...")
    print("=" * 50)

    result = system.run(force=args.force, skip_email=args.skip_email)

    print(f"\nRun Complete")
    print("-" * 30)
    print(f"Videos found: {result['videos_found']}")
    print(f"Videos processed: {result['videos_processed']}")
    print(f"Videos skipped (no transcript): {result['videos_skipped']}")
    print(f"Errors: {result['errors']}")
    print(f"Email sent: {result['email_sent']}")

    if result.get('error'):
        print(f"\nError: {result['error']}")
        sys.exit(1)


if __name__ == "__main__":
    main()

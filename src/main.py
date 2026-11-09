"""
Main entry point for the YouTube Summary System.
Orchestrates all components: monitoring, extraction, summarization, and notification.
"""

import sys
import json
import time
import argparse
import hashlib
from datetime import datetime
from itertools import islice
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# Add src directory to path for imports
sys.path.insert(0, str(Path(__file__).parent))

from utils import (
    atomic_write_text,
    setup_logging,
    load_config,
    load_channels,
    generate_summary_filename,
    get_summaries_dir
)
from state_manager import StateManager
from processing_lock import check_claim
from channel_monitor import ChannelMonitor, VideoInfo
from historical_fetcher import HistoricalFetcher
from transcript_extractor import TranscriptExtractor
from summarizer import Summarizer
from email_notifier import EmailNotifier
from scheduler import Scheduler
from realtime_fallback import PlaybackQueue, enabled as realtime_enabled
from transcript_acquisition import from_extractor, from_job, summary_metadata, write_metadata

logger = setup_logging("main")


def _summary_matches(path: Path, video_id: str, *, legacy: bool) -> bool:
    if not path.is_file():
        return False
    sidecar = path.with_suffix('.meta.json')
    try:
        if sidecar.is_file():
            metadata = json.loads(sidecar.read_text(encoding='utf-8'))
            if not isinstance(metadata, dict) or metadata.get('video_id') != video_id:
                return False
            digest = metadata.get('summary_sha256')
            return not digest or hashlib.sha256(path.read_bytes()).hexdigest() == digest
        if legacy:
            with path.open(encoding='utf-8') as file:
                for line in islice(file, 20):
                    if line.startswith('URL:'):
                        url = urlparse(line.removeprefix('URL:').strip())
                        return (url.netloc in {'youtube.com', 'www.youtube.com', 'm.youtube.com'}
                                and parse_qs(url.query).get('v') == [video_id]
                                or url.netloc == 'youtu.be' and url.path == f'/{video_id}')
    except (ValueError, FileNotFoundError):
        return False
    return False


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
        self.playback = PlaybackQueue() if realtime_enabled() else None
        self._captions_blocked = False

    def _ensure_summarizer(self):
        """Initialize summarizer on demand."""
        if self.summarizer is None:
            try:
                self.summarizer = Summarizer()
            except ValueError as e:
                logger.error(f"Failed to initialize summarizer: {e}")
                raise

    def process_video(self, video: VideoInfo, *, force=False) -> dict:
        """Deliver the daily summary once per video, including concurrent callers."""
        result = {
            "video_id": video.video_id,
            "title": video.title,
            "channel": video.channel_name,
            "success": False,
            "error": None,
            "summary_file": None,
            "summary_path": None,
        }
        vid = video.video_id
        with self.state_manager.processing_claim(vid, 'daily') as claim:
            if claim is None:
                result['skipped'] = 'in_progress'
                return result

            try:
                self.state_manager.refresh()
                prior = self.state_manager.get_video_metadata(vid, 'daily')
                delivered = self.playback.delivery_output(vid, 'daily') if self.playback else None
                pending = self.playback and self.playback.needs_delivery(vid, 'daily')
                directory = get_summaries_dir(channel_handle=video.channel_handle)
                filename = generate_summary_filename(video.channel_name, video.title,
                                                     video.published_date, video_id=vid)
                expected = directory / filename
                if not force and not pending and prior:
                    stored = Path(delivered).name if delivered else prior['summary_file']
                    result.update(success=True, skipped='already_completed',
                                  summary_file=stored, summary_path=str(directory / stored))
                    return result

                legacy = generate_summary_filename(video.channel_name, video.title, video.published_date)
                candidates = [expected]
                if prior and prior['summary_file']:
                    candidates.append(directory / Path(prior['summary_file']).name)
                if delivered:
                    candidates.append(directory / Path(delivered).name)
                suffix = f"_{vid.encode('utf-8').hex()}.txt"
                candidates.extend(directory.glob(f'*{suffix}'))
                candidates.append(directory / legacy)
                existing = next((path for path in dict.fromkeys(candidates)
                                 if _summary_matches(path, vid, legacy=not path.name.endswith(suffix))), None)
                if existing is not None and not force:
                    check_claim(claim)
                    if pending:
                        self.playback.delivered(vid, 'daily', existing)
                    self._record_daily_output(video, existing)
                    result.update(success=True, skipped='already_completed',
                                  summary_file=existing.name, summary_path=str(existing))
                    return result
                if not force and delivered:
                    result.update(success=True, skipped='already_completed',
                                  summary_file=Path(delivered).name, summary_path=delivered)
                    return result

                return self._process_video(video, result, existing or expected, claim)
            except OSError as error:
                result['error'] = f'Failed to save summary: {error}'
                logger.error('Daily output failed for %s: %s', vid, error)
                return result

    def _record_daily_output(self, video, summary_path):
        self.state_manager.mark_video_processed(
            video_id=video.video_id,
            title=video.title,
            channel=video.channel_name,
            summary_file=summary_path.name,
            published_date=video.published_date.strftime('%Y-%m-%d'),
            destination='daily',
        )

    def _process_video(self, video, result, summary_path, claim):
        logger.info(f"Processing: {video.title} ({video.video_id})")

        # Extract transcript
        job = self.playback.get(video.video_id) if self.playback else None
        if job and job['status'] == 'ready':
            transcript, status = job['transcript'], 'ok'
        elif job and job['status'] == 'recording':
            transcript, status = None, 'deferred'
        elif self._captions_blocked and self.playback:
            transcript, status = None, 'blocked'
        else:
            transcript, status = self.transcript_extractor.extract_with_status(video.video_id)
        if status == 'blocked':
            self._captions_blocked = True
        if self.playback and status in {'blocked', 'no_captions', 'deferred'}:
            self.playback.enqueue({
                'video_id': video.video_id, 'title': video.title,
                'channel_title': video.channel_name, 'channel_id': video.channel_id,
                'channel_handle': video.channel_handle,
                'published_at': video.published_date.isoformat(), 'url': video.url,
            }, f'captions_{status}', 'daily')
            result['error'] = 'Awaiting realtime playback'
            result['next_action'] = self.playback.get(video.video_id)['next_action']
            return result
        if not transcript:
            result['error'] = ('No transcript available' if status in {'no_captions', 'unavailable'}
                               else f'Transcript {status}')
            logger.warning('Transcript unavailable for %s: %s', video.video_id, status)
            return result

        # Generate summary
        source = from_job(job) if job and job['status'] == 'ready' else from_extractor(self.transcript_extractor)
        check_claim(claim)
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
        metadata = summary_metadata(video.video_id, transcript, sections, source)
        summary_text += '\n검수 상태: 확인 대기 (발언자·고유명사·수치 대조 필요)\n'

        check_claim(claim)
        metadata['summary_sha256'] = hashlib.sha256(summary_text.encode('utf-8')).hexdigest()
        metadata_path = write_metadata(summary_path, metadata)
        atomic_write_text(summary_path, summary_text)
        if job and job['status'] == 'ready':
            self.playback.delivered(video.video_id, 'daily', summary_path)
        elif job:
            self.playback.recover_with_captions(video.video_id, transcript, 'daily', summary_path, source)
        self._record_daily_output(video, summary_path)
        logger.info(f"Summary saved: {summary_path}")
        result.update(success=True, summary_file=summary_path.name, summary_path=str(summary_path),
                      metadata_path=str(metadata_path), acquisition=source['source'], review=metadata['review'])

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
            "videos_deferred": 0,
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
            processed_ids = self.state_manager.get_all_processed_video_ids('daily')
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

                if result.get('skipped') == 'in_progress':
                    run_result['videos_deferred'] += 1
                elif result.get('skipped'):
                    run_result['videos_skipped'] += 1
                elif result["success"]:
                    run_result["videos_processed"] += 1
                    run_result["processed_videos"].append({
                        "video_id": result["video_id"],
                        "title": result["title"],
                        "channel": result["channel"],
                        "summary_file": result["summary_file"],
                        "summary_path": result.get("summary_path"),
                        "published_date": video.published_date.strftime("%Y-%m-%d")
                    })
                elif result["error"] == "Awaiting realtime playback":
                    run_result["videos_deferred"] += 1
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
                errors=run_result["errors"],
                deferred=run_result["videos_deferred"]
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

    @staticmethod
    def video_from_job(job) -> VideoInfo:
        row = job['payload']
        return VideoInfo(
            video_id=row['video_id'], title=row['title'],
            channel_name=row['channel_title'], channel_id=row.get('channel_id') or '',
            published_date=datetime.fromisoformat(row['published_at'].replace('Z', '+00:00')),
            url=row['url'], channel_handle=row.get('channel_handle'))

    def resume_realtime(self):
        from queue_consumer import _REQUEST_DELAY, drop_expired_jobs

        results = []
        if self.playback:
            drop_expired_jobs(self.playback)
            for job in self.playback.ready('daily') + self.playback.waiting('daily'):
                if job['status'] != 'ready':
                    if self._captions_blocked:
                        break
                    time.sleep(_REQUEST_DELAY)
                results.append(self.process_video(self.video_from_job(job)))
        return results

    def deliver_ready(self, video_ids: set) -> dict:
        """Summarize played-back videos into the NarrativeRadar inbox and summaries/."""
        from queue_consumer import run as run_queue

        # A manually imported UI transcript may originally have only a narrative
        # destination. An explicit delivery request also asks for the daily file.
        self.state_manager.refresh()
        for vid in video_ids:
            job = self.playback.get(vid) if self.playback else None
            if (job and job['status'] == 'ready'
                    and not self.state_manager.is_video_processed(vid, 'daily')):
                self.playback.enqueue(job['payload'], job['reason'], 'daily')
        queue_result = run_queue(only=video_ids)
        daily = [self.process_video(self.video_from_job(job))
                 for job in self.playback.ready('daily') if job['video_id'] in video_ids]
        return {"narrative_written": queue_result["written"],
                "daily_written": sum(r["success"] and not r.get('skipped') for r in daily),
                "errors": queue_result["errors"] + sum(not r["success"] and not r.get('skipped') for r in daily),
                "busy": queue_result['busy'] + sum(r.get('skipped') == 'in_progress' for r in daily)}

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
            "videos_deferred": 0,
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
                already_processed = self.state_manager.is_video_processed(video.video_id, 'daily')
                status = "[SKIP - already processed]" if already_processed and not reprocess else "[WOULD PROCESS]"
                print(f"  {status} {video.published_date.strftime('%Y-%m-%d')} - {video.title}")
            return run_result

        # Process each video
        for video in videos:
            # Check if already processed (unless reprocess flag is set)
            if not reprocess and self.state_manager.is_video_processed(video.video_id, 'daily'):
                logger.info(f"Skipping already processed: {video.title}")
                run_result["videos_skipped"] += 1
                continue

            result = self.process_video(video, force=reprocess)

            if result.get('skipped') == 'in_progress':
                run_result['videos_deferred'] += 1
            elif result.get('skipped'):
                run_result['videos_skipped'] += 1
            elif result["success"]:
                run_result["videos_processed"] += 1
                run_result["processed_videos"].append({
                    "video_id": result["video_id"],
                    "title": result["title"],
                    "channel": result["channel"],
                    "summary_file": result["summary_file"],
                    "summary_path": result.get("summary_path"),
                    "published_date": video.published_date.strftime("%Y-%m-%d")
                })
            elif result["error"] == "Awaiting realtime playback":
                run_result["videos_deferred"] += 1
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

    # Queue mode: consume NarrativeRadar's discovery work-queue and emit
    # contract JSON into the shared inbox (no channel monitoring / no email).
    parser.add_argument(
        "--queue",
        action="store_true",
        help="Consume the NarrativeRadar work-queue → contract JSON inbox"
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max videos to process in queue mode (default: all)"
    )

    parser.add_argument('--resume-realtime', action='store_true',
                        help='Summarize verified Mac transcripts without new discovery or email')
    parser.add_argument('--candidates', type=int, metavar='N',
                        help='Print the newest N unprocessed in-window queue videos as JSON')
    parser.add_argument('--deliver-ready', metavar='IDS',
                        help='Summarize played-back videos (comma-separated IDs) into both outputs')
    args = parser.parse_args()
    if args.candidates:
        from queue_consumer import candidates
        print(json.dumps(candidates(args.candidates), ensure_ascii=False))
        return
    if args.deliver_ready:
        result = YouTubeSummarySystem().deliver_ready(set(filter(None, args.deliver_ready.split(','))))
        print(json.dumps(result, ensure_ascii=False))
        if result["errors"]:
            sys.exit(1)
        return
    if args.resume_realtime:
        results = YouTubeSummarySystem().resume_realtime()
        print(f"Realtime summaries: {sum(r['success'] and not r.get('skipped') for r in results)}/{len(results)}")
        for result in results:
            print(result)
        if any(not r['success'] and not r.get('skipped') for r in results):
            sys.exit(1)
        return

    # Handle queue mode (NarrativeRadar bridge)
    if args.queue:
        from queue_consumer import run as run_queue

        print("\nYouTube Queue Consumer (NarrativeRadar bridge)")
        print("=" * 50)
        res = run_queue(limit=args.limit, force=args.reprocess)
        print(f"Queued:        {res['queued']}")
        print(f"Written:       {res['written']}")
        print(f"Caption used:  {res.get('caption_used', 0)}")
        print(f"UI caption used: {res.get('ui_used', 0)}")
        print(f"Whisper used:  {res.get('whisper_used', 0)}")
        print(f"Realtime used: {res.get('realtime_used', 0)}")
        print(f"Awaiting transcript recovery: {res.get('deferred', 0)}")
        print(f"In progress:   {res['busy']}")
        print(f"Skipped:       {res['skipped']}")
        print(f"No captions:   {res['no_transcript']}")
        print(f"Unavailable:   {res.get('unavailable', 0)}")
        print(f"Too old:       {res.get('too_old', 0)}")
        print(f"Errors:        {res['errors']}")
        if res.get("blocked"):
            print("\n[!] Aborted early: YouTube IP-blocked transcript requests.")
            print("  Remaining videos are queued for official UI captions or supervised playback.")
            print("  Inspect next_action with realtime_worker.py list; captions may also be retried later.")
            sys.exit(2)
        # Queue mode is the only scheduled run while the daily DAG is paused, so it
        # also clears daily-destination jobs. A bounded --limit run stays bounded.
        if args.limit is None:
            print(f"Daily resumed: {len(YouTubeSummarySystem().resume_realtime())}")
        # Per-video transient errors are expected (and retried next run, since the
        # queue file is now retained when errors>0). Only fail the whole job when
        # NOTHING was written despite errors — i.e. the entire batch failed —
        # so a partial-success run (e.g. 37 written / 5 errored) is not marked
        # FAILED in Airflow.
        if res["errors"] and res["written"] == 0:
            sys.exit(1)
        return

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
            processed_ids = state.get_all_processed_video_ids('daily')
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
    print(f"Awaiting transcript recovery: {result['videos_deferred']}")
    print(f"Errors: {result['errors']}")
    print(f"Email sent: {result['email_sent']}")

    if result.get('error'):
        print(f"\nError: {result['error']}")
        sys.exit(1)


if __name__ == "__main__":
    main()

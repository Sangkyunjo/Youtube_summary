"""
YouTube Summary System - Airflow DAG

This DAG orchestrates the YouTube channel monitoring and summarization pipeline:
1. Get new videos from monitored channels
2. Extract transcripts and generate summaries
3. Send email report with results

Schedule: Daily at 11:00 PM
Catchup: Enabled (will backfill missed runs)
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path

from airflow import DAG
from airflow.operators.python import PythonOperator, ShortCircuitOperator
from airflow.operators.empty import EmptyOperator
from airflow.utils.trigger_rule import TriggerRule

# Project paths - supports both local and Docker environments
# Docker mounts (existing airflow-docker): /opt/airflow/youtube_summary/src, etc.
# Docker mounts (standalone): /opt/airflow/src, /opt/airflow/config, etc.
# Local: C:/Users/trill/Youtube_summary/src
def _get_project_paths():
    """Determine project paths based on environment."""
    # Check for existing airflow-docker setup (integrated)
    docker_integrated_src = Path("/opt/airflow/youtube_summary/src")
    # Check for standalone docker setup
    docker_standalone_src = Path("/opt/airflow/src")
    # Local development
    local_src = Path("C:/Users/trill/Youtube_summary/src")

    if docker_integrated_src.exists():
        return Path("/opt/airflow/youtube_summary"), docker_integrated_src
    elif docker_standalone_src.exists():
        return Path("/opt/airflow"), docker_standalone_src
    return Path("C:/Users/trill/Youtube_summary"), local_src

PROJECT_DIR, SRC_DIR = _get_project_paths()

# Default arguments for all tasks
default_args = {
    "owner": "airflow",
    "depends_on_past": False,
    "email_on_failure": True,
    "email_on_retry": False,
    "retries": 2,
    "retry_delay": timedelta(minutes=5),
    "execution_timeout": timedelta(minutes=30),
}


def _setup_path():
    """Add project src directory to Python path and load environment."""
    src_path = str(SRC_DIR)
    if src_path not in sys.path:
        sys.path.insert(0, src_path)

    # Load .env file if exists (for local development)
    env_file = PROJECT_DIR / "config" / ".env"
    if env_file.exists():
        from dotenv import load_dotenv
        load_dotenv(env_file)


def check_channels_configured(**context) -> bool:
    """
    Check if there are channels configured to monitor.
    ShortCircuitOperator will skip downstream tasks if False.
    """
    _setup_path()
    from utils import load_channels

    channels = load_channels()
    num_channels = len(channels) if channels else 0

    context["ti"].xcom_push(key="num_channels", value=num_channels)

    if num_channels == 0:
        print("No channels configured in channels.yaml - skipping run")
        return False

    print(f"Found {num_channels} channels to monitor")
    return True


def get_new_videos(**context) -> list:
    """
    Task 1: Fetch new videos from all monitored YouTube channels.

    Uses RSS feeds to get recent videos and filters out already-processed ones.
    Returns list of video metadata via XCom for the next task.
    """
    _setup_path()
    from channel_monitor import ChannelMonitor
    from state_manager import StateManager

    monitor = ChannelMonitor()
    state = StateManager()

    # Get already processed video IDs
    processed_ids = state.get_all_processed_video_ids()
    print(f"Already processed {len(processed_ids)} videos")

    # Get new videos from all channels
    new_videos = monitor.get_all_new_videos(processed_ids)
    print(f"Found {len(new_videos)} new videos to process")

    if not new_videos:
        return []

    # Serialize video info for XCom (can't pass complex objects)
    video_list = []
    for video in new_videos:
        video_list.append({
            "video_id": video.video_id,
            "title": video.title,
            "channel_name": video.channel_name,
            "published_date": video.published_date.isoformat(),
            "url": video.url,
        })
        print(f"  - [{video.channel_name}] {video.title}")

    return video_list


def process_videos(**context) -> dict:
    """
    Task 2: Process each video - extract transcript and generate summary.

    For each video:
    1. Extract transcript (Korean preferred, English fallback)
    2. Generate AI summary using Claude API
    3. Save summary file to configured directory
    4. Mark video as processed in state

    Returns processing results for email report.
    """
    _setup_path()
    from datetime import datetime as dt
    from channel_monitor import VideoInfo
    from transcript_extractor import TranscriptExtractor
    from summarizer import Summarizer
    from state_manager import StateManager
    from utils import generate_summary_filename, get_summaries_dir

    # Get videos from previous task
    ti = context["ti"]
    video_list = ti.xcom_pull(task_ids="get_new_videos")

    if not video_list:
        print("No videos to process")
        return {
            "videos_found": 0,
            "videos_processed": 0,
            "videos_skipped": 0,
            "errors": 0,
            "processed_videos": [],
        }

    # Initialize components
    extractor = TranscriptExtractor()
    summarizer = None  # Lazy initialization
    state = StateManager()
    summaries_dir = get_summaries_dir()

    results = {
        "videos_found": len(video_list),
        "videos_processed": 0,
        "videos_skipped": 0,
        "errors": 0,
        "processed_videos": [],
    }

    for v in video_list:
        video_id = v["video_id"]
        title = v["title"]
        channel_name = v["channel_name"]
        published_date = dt.fromisoformat(v["published_date"])
        url = v["url"]

        print(f"\nProcessing: {title} ({video_id})")

        # Reconstruct VideoInfo object
        video = VideoInfo(
            video_id=video_id,
            title=title,
            channel_name=channel_name,
            published_date=published_date,
            url=url,
        )

        # Step 1: Extract transcript
        try:
            transcript = extractor.extract(video_id)
            if not transcript:
                print(f"  No transcript available - skipping")
                results["videos_skipped"] += 1
                continue
            print(f"  Transcript extracted: {len(transcript)} characters")
        except Exception as e:
            print(f"  Transcript extraction failed: {e}")
            results["errors"] += 1
            continue

        # Step 2: Generate summary (lazy init summarizer)
        try:
            if summarizer is None:
                summarizer = Summarizer()

            sections = summarizer.summarize(
                transcript=transcript,
                title=title,
                channel=channel_name,
            )

            if not sections:
                print(f"  Summarization returned empty - skipping")
                results["errors"] += 1
                continue

            print(f"  Summary generated with {len(sections)} sections")
        except Exception as e:
            print(f"  Summarization failed: {e}")
            results["errors"] += 1
            continue

        # Step 3: Create and save summary file
        try:
            summary_text = summarizer.create_summary_text(
                title=title,
                channel=channel_name,
                published=published_date.strftime("%Y-%m-%d %H:%M"),
                url=url,
                sections=sections,
            )

            filename = generate_summary_filename(
                channel_name=channel_name,
                video_title=title,
                published_date=published_date,
            )

            summary_path = summaries_dir / filename

            with open(summary_path, "w", encoding="utf-8") as f:
                f.write(summary_text)

            print(f"  Summary saved: {filename}")
        except Exception as e:
            print(f"  Failed to save summary: {e}")
            results["errors"] += 1
            continue

        # Step 4: Mark as processed
        try:
            state.mark_video_processed(
                video_id=video_id,
                title=title,
                channel=channel_name,
                summary_file=filename,
                published_date=published_date.strftime("%Y-%m-%d"),
            )
        except Exception as e:
            print(f"  Warning: Failed to mark as processed: {e}")

        # Record success
        results["videos_processed"] += 1
        results["processed_videos"].append({
            "video_id": video_id,
            "title": title,
            "channel": channel_name,
            "summary_file": filename,
            "published_date": published_date.strftime("%Y-%m-%d"),
        })

    print(f"\nProcessing complete:")
    print(f"  Found: {results['videos_found']}")
    print(f"  Processed: {results['videos_processed']}")
    print(f"  Skipped (no transcript): {results['videos_skipped']}")
    print(f"  Errors: {results['errors']}")

    return results


def send_email_report(**context):
    """
    Task 3: Send email report with processed videos.

    Only sends if there are successfully processed videos.
    Uses the existing EmailNotifier for consistent formatting.
    """
    _setup_path()
    from email_notifier import EmailNotifier

    ti = context["ti"]
    process_result = ti.xcom_pull(task_ids="process_videos")

    if not process_result:
        print("No processing results available")
        return

    processed_videos = process_result.get("processed_videos", [])

    if not processed_videos:
        print("No videos were processed - skipping email")
        return

    print(f"Sending email report for {len(processed_videos)} videos")

    try:
        notifier = EmailNotifier()
        if notifier.enabled:
            success = notifier.send_daily_report(processed_videos)
            if success:
                print("Email report sent successfully")
            else:
                print("Email report failed to send")
        else:
            print("Email notifications are disabled")
    except Exception as e:
        print(f"Failed to send email report: {e}")
        raise


def cleanup_old_state(**context):
    """
    Task 4: Cleanup old state entries (older than 90 days).

    Runs after email to keep state files manageable.
    """
    _setup_path()
    from state_manager import StateManager

    state = StateManager()
    state.cleanup_old_entries(days=90)
    print("State cleanup complete")


def record_run_status(**context):
    """
    Final task: Record run completion status.

    Updates last_run.json with run results for monitoring.
    """
    _setup_path()
    from state_manager import StateManager

    ti = context["ti"]
    process_result = ti.xcom_pull(task_ids="process_videos")

    state = StateManager()

    if process_result:
        videos_processed = process_result.get("videos_processed", 0)
        errors = process_result.get("errors", 0)
        state.record_run_complete(
            videos_processed=videos_processed,
            errors=errors,
        )
        print(f"Run recorded: {videos_processed} processed, {errors} errors")
    else:
        state.record_run_complete(videos_processed=0, errors=0)
        print("Run recorded: no videos processed")


def on_failure_callback(context):
    """Callback for task failures - sends error notification."""
    _setup_path()
    from email_notifier import EmailNotifier

    exception = context.get("exception")
    task_id = context.get("task_instance").task_id
    error_msg = f"Task '{task_id}' failed: {exception}"

    print(f"Task failure: {error_msg}")

    try:
        notifier = EmailNotifier()
        if notifier.enabled:
            notifier.send_error_notification(error_msg)
    except Exception as e:
        print(f"Failed to send error notification: {e}")


# Create the DAG
with DAG(
    dag_id="youtube_summary_system",
    default_args=default_args,
    description="YouTube Channel Monitoring and Summarization System",
    schedule_interval="0 23 * * *",  # Daily at 11:00 PM
    start_date=datetime(2026, 1, 1),
    catchup=True,  # Enable backfill for missed runs
    max_active_runs=1,  # Only one run at a time
    tags=["youtube", "summarization", "claude"],
    doc_md=__doc__,
) as dag:

    # Task: Start marker
    start = EmptyOperator(
        task_id="start",
    )

    # Task: Check if channels are configured
    check_channels = ShortCircuitOperator(
        task_id="check_channels",
        python_callable=check_channels_configured,
        on_failure_callback=on_failure_callback,
    )

    # Task: Get new videos from channels
    get_videos = PythonOperator(
        task_id="get_new_videos",
        python_callable=get_new_videos,
        on_failure_callback=on_failure_callback,
    )

    # Task: Process videos (extract + summarize)
    process = PythonOperator(
        task_id="process_videos",
        python_callable=process_videos,
        execution_timeout=timedelta(hours=2),  # Longer timeout for processing
        on_failure_callback=on_failure_callback,
    )

    # Task: Send email report
    send_email = PythonOperator(
        task_id="send_email_report",
        python_callable=send_email_report,
        on_failure_callback=on_failure_callback,
    )

    # Task: Cleanup old state entries
    cleanup = PythonOperator(
        task_id="cleanup_old_state",
        python_callable=cleanup_old_state,
        trigger_rule=TriggerRule.ALL_DONE,  # Run even if previous tasks failed
    )

    # Task: Record run status
    record_status = PythonOperator(
        task_id="record_run_status",
        python_callable=record_run_status,
        trigger_rule=TriggerRule.ALL_DONE,  # Always record status
    )

    # Task: End marker
    end = EmptyOperator(
        task_id="end",
        trigger_rule=TriggerRule.ALL_DONE,
    )

    # Define task dependencies
    # fmt: off
    (
        start
        >> check_channels
        >> get_videos
        >> process
        >> send_email
        >> cleanup
        >> record_status
        >> end
    )
    # fmt: on

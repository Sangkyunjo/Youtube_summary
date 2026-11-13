"""
YouTube Summary System - Airflow DAG

This DAG orchestrates the YouTube channel monitoring and summarization pipeline:
1. Get new videos from monitored channels
2. Extract transcripts and generate summaries
3. Send email report with results

Schedule: Daily at 15:00 KST
Catchup: Disabled
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
    # The shared Airflow PYTHONPATH also lists FinanceHub, which owns a top-level
    # `utils` package. This project's helpers are `ytsum_utils` (renamed from
    # `utils` on 2026-11-13) so neither project has to evict the other's module.
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
    from ytsum_utils import load_channels

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

    Uses the channel monitor and filters out already-processed videos.
    Returns list of video metadata via XCom for the next task.
    """
    _setup_path()
    from channel_monitor import ChannelMonitor
    from state_manager import StateManager

    monitor = ChannelMonitor()
    state = StateManager()

    # Get already processed video IDs
    processed_ids = state.get_all_processed_video_ids('daily')
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
            "channel_id": video.channel_id,
            "channel_handle": video.channel_handle,
            "published_date": video.published_date.isoformat(),
            "url": video.url,
        })
        print(f"  - [{video.channel_name}] {video.title}")

    return video_list


def process_videos(**context) -> dict:
    """Use the same transcript recovery and summarization as the daily CLI."""
    _setup_path()
    from channel_monitor import VideoInfo
    from main import YouTubeSummarySystem

    video_list = context["ti"].xcom_pull(task_ids="get_new_videos") or []
    results = {
        "videos_found": len(video_list),
        "videos_processed": 0,
        "videos_skipped": 0,
        "videos_deferred": 0,
        "errors": 0,
        "processed_videos": [],
    }
    if not video_list:
        print("No videos to process")
        return results

    system = YouTubeSummarySystem()
    for v in video_list:
        video = VideoInfo(
            video_id=v["video_id"],
            title=v["title"],
            channel_name=v["channel_name"],
            channel_id=v.get("channel_id", ""),
            channel_handle=v.get("channel_handle"),
            published_date=datetime.fromisoformat(v["published_date"]),
            url=v["url"],
        )
        try:
            result = system.process_video(video)
        except Exception as e:
            print(f"  Processing failed for {video.video_id}: {e}")
            results["errors"] += 1
            continue

        if result.get('skipped') == 'in_progress':
            results['videos_deferred'] += 1
        elif result.get('skipped'):
            results['videos_skipped'] += 1
        elif result["success"]:
            results["videos_processed"] += 1
            results["processed_videos"].append({
                "video_id": video.video_id,
                "title": video.title,
                "channel": video.channel_name,
                "summary_file": result["summary_file"],
                "summary_path": result["summary_path"],
                "published_date": video.published_date.strftime("%Y-%m-%d"),
            })
        elif result["error"] == "Awaiting realtime playback":
            results["videos_deferred"] += 1
        elif result["error"] == "No transcript available":
            results["videos_skipped"] += 1
        else:
            print(f"  Processing failed for {video.video_id}: {result['error']}")
            results["errors"] += 1

    print("\nProcessing complete:")
    print(f"  Found: {results['videos_found']}")
    print(f"  Processed: {results['videos_processed']}")
    print(f"  Skipped (no transcript): {results['videos_skipped']}")
    print(f"  Deferred: {results['videos_deferred']}")
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
        deferred = process_result.get("videos_deferred", 0)
        state.record_run_complete(
            videos_processed=videos_processed,
            errors=errors,
            deferred=deferred,
        )
        print(f"Run recorded: {videos_processed} processed, {deferred} deferred, {errors} errors")
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
    schedule_interval='0 6 * * *',  # 2026-10-21 daytime collection policy
    start_date=datetime(2026, 1, 1),
    catchup=False,
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

# daytime runtime gate — all scheduled collection roots wait for 09:00 KST.
import sys as _daytime_sys
_daytime_sys.path.insert(0, '/opt/airflow/dags/dataconductor')
from daytime_support import apply_daytime as _apply_daytime
for _daytime_dag in list(globals().values()):
    if isinstance(_daytime_dag, DAG):
        _apply_daytime(_daytime_dag)

"""
Scheduler module with catch-up logic for missed runs.
Determines if the system should run and handles missed runs.
"""

from datetime import datetime, timedelta
from typing import Optional

from utils import load_config, setup_logging
from state_manager import StateManager

logger = setup_logging("scheduler")


class Scheduler:
    """Manages scheduling and catch-up logic for the YouTube Summary System."""

    def __init__(self, state_manager: StateManager = None):
        self.config = load_config()
        self.schedule_config = self.config.get("schedule", {})

        self.catch_up_enabled = self.schedule_config.get("catch_up_enabled", True)
        self.max_video_age_days = self.schedule_config.get("max_video_age_days", 7)

        self.state_manager = state_manager or StateManager()

    def should_run(self) -> tuple[bool, str]:
        """
        Determine if the system should run now.

        Returns:
            Tuple of (should_run, reason)
        """
        last_run_time = self.state_manager.get_last_run_time()

        if last_run_time is None:
            return True, "First run - no previous run recorded"

        now = datetime.now()
        time_since_last_run = now - last_run_time

        # If last run was today, skip (unless forced)
        if last_run_time.date() == now.date():
            last_status = self.state_manager.get_last_run_status()
            if last_status == "success":
                return False, f"Already ran successfully today at {last_run_time.strftime('%H:%M:%S')}"
            else:
                return True, f"Previous run today had status: {last_status}"

        # Check if we need catch-up
        if self.catch_up_enabled:
            days_missed = time_since_last_run.days
            if days_missed > 1:
                return True, f"Catch-up run - {days_missed} days since last run"

        return True, "Regular scheduled run"

    def get_catchup_info(self) -> dict:
        """
        Get information about potential catch-up runs.

        Returns:
            Dictionary with catch-up information
        """
        last_run_time = self.state_manager.get_last_run_time()
        now = datetime.now()

        if last_run_time is None:
            return {
                "needs_catchup": True,
                "reason": "No previous run recorded",
                "days_missed": self.max_video_age_days,
                "last_run": None
            }

        time_since_last_run = now - last_run_time
        days_missed = time_since_last_run.days

        return {
            "needs_catchup": days_missed > 1,
            "reason": f"{days_missed} days since last run" if days_missed > 1 else "Up to date",
            "days_missed": days_missed,
            "last_run": last_run_time.isoformat()
        }

    def get_video_cutoff_date(self) -> datetime:
        """
        Get the cutoff date for processing videos.
        Videos older than this should be skipped.

        Returns:
            Cutoff datetime
        """
        return datetime.now() - timedelta(days=self.max_video_age_days)

    def log_run_decision(self) -> bool:
        """
        Log the run decision and return whether to proceed.

        Returns:
            True if should run, False otherwise
        """
        should_run, reason = self.should_run()

        if should_run:
            logger.info(f"Proceeding with run: {reason}")
        else:
            logger.info(f"Skipping run: {reason}")

        return should_run

    def get_schedule_status(self) -> dict:
        """
        Get the current schedule status.

        Returns:
            Dictionary with schedule status information
        """
        last_run_time = self.state_manager.get_last_run_time()
        last_run_status = self.state_manager.get_last_run_status()
        catchup_info = self.get_catchup_info()

        status = {
            "last_run_time": last_run_time.isoformat() if last_run_time else None,
            "last_run_status": last_run_status,
            "catch_up_enabled": self.catch_up_enabled,
            "max_video_age_days": self.max_video_age_days,
            "catchup_info": catchup_info
        }

        should_run, reason = self.should_run()
        status["should_run_now"] = should_run
        status["run_reason"] = reason

        return status


def main():
    """Test the scheduler."""
    scheduler = Scheduler()

    print("Schedule Status:")
    print("-" * 40)

    status = scheduler.get_schedule_status()

    print(f"Last run time: {status['last_run_time'] or 'Never'}")
    print(f"Last run status: {status['last_run_status'] or 'N/A'}")
    print(f"Catch-up enabled: {status['catch_up_enabled']}")
    print(f"Max video age: {status['max_video_age_days']} days")
    print(f"Should run now: {status['should_run_now']}")
    print(f"Reason: {status['run_reason']}")

    print("\nCatch-up Info:")
    catchup = status['catchup_info']
    print(f"  Needs catch-up: {catchup['needs_catchup']}")
    print(f"  Days missed: {catchup['days_missed']}")
    print(f"  Reason: {catchup['reason']}")


if __name__ == "__main__":
    main()

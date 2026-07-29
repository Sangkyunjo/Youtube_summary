"""RETIRED 2026-07-29 — do not re-enable without reading this.

Parked here (and excluded via ../.airflowignore) rather than deleted, because
the collectors it calls are still live code in WarAlarm.

Why it never worked: it imports WarAlarm's backend into the *Airflow*
interpreter, and that backend is SQLAlchemy 2.0 code
(`from sqlalchemy.orm import DeclarativeBase`). Airflow 2.8 hard-pins
SQLAlchemy <2.0, and `DeclarativeBase` simply does not exist there — unlike
the FinanceHub collectors, whose 2.0 idioms could be bridged with
`future=True` + a pandas pin, this one cannot be bridged at all without
rewriting WarAlarm's ORM base.

Result: every run failed from the start. 1,111 failures per task across six
tasks, hourly, with zero successes ever recorded — the DAG had no working run
in its entire history.

It was also redundant. WarAlarm collection already runs through DataConductor
job WAR-01, which POSTs /api/v1/collect/trigger and lets the celery worker do
the work inside WarAlarm's own image, where SQLAlchemy 2.0 is installed. That
path works; this one duplicated it and could not.

To revive: run the collectors in an image that carries SQLAlchemy 2.0 (the
same reasoning behind docker-compose.collector.yml for FinanceHub), never by
importing them into the Airflow interpreter.
"""

"""
SENTINEL Collection DAG — Runs data collectors on schedule and computes threat scores.
Integrates with the WarAlarm/SENTINEL backend.
"""
import sys
from datetime import datetime, timedelta

from airflow import DAG
from airflow.operators.python import PythonOperator

# Add SENTINEL backend to Python path
SENTINEL_BACKEND = "/opt/airflow/sentinel/backend"

default_args = {
    "owner": "sentinel",
    "depends_on_past": False,
    "email_on_failure": False,
    "retries": 2,
    "retry_delay": timedelta(minutes=5),
}


def _ensure_path():
    if SENTINEL_BACKEND not in sys.path:
        sys.path.insert(0, SENTINEL_BACKEND)


def collect_diplomatic_task():
    _ensure_path()
    from app.collectors.diplomatic import run
    return run()


def collect_notam_task():
    _ensure_path()
    from app.collectors.notam import run
    return run()


def collect_sns_task():
    _ensure_path()
    from app.collectors.sns import run
    return run()


def collect_telegram_task():
    _ensure_path()
    from app.collectors.telegram_osint import run
    return run()


def collect_maritime_task():
    _ensure_path()
    from app.collectors.maritime import run
    return run()


def collect_cyber_task():
    _ensure_path()
    from app.collectors.cyber import run
    return run()


def compute_threats_task():
    _ensure_path()
    from app.core.database import SessionLocal
    from app.models.tables import Region
    from app.anomaly.detectors import compute_threat_for_region

    db = SessionLocal()
    try:
        regions = db.query(Region).all()
        for region in regions:
            result = compute_threat_for_region(db, region.id)
            print(f"[sentinel] {region.code}: {result['label']} ({result['composite_score']}%)")
    finally:
        db.close()


with DAG(
    dag_id="sentinel_collection",
    default_args=default_args,
    description="SENTINEL OSINT collector pipeline — diplomatic, NOTAM, SNS, Telegram, Maritime, Cyber",
    schedule_interval="0 * * * *",  # Every hour
    start_date=datetime(2026, 3, 5),
    catchup=False,
    tags=["sentinel", "osint"],
) as dag:

    diplomatic = PythonOperator(
        task_id="collect_diplomatic",
        python_callable=collect_diplomatic_task,
    )

    notam = PythonOperator(
        task_id="collect_notam",
        python_callable=collect_notam_task,
    )

    sns = PythonOperator(
        task_id="collect_sns",
        python_callable=collect_sns_task,
    )

    telegram = PythonOperator(
        task_id="collect_telegram",
        python_callable=collect_telegram_task,
    )

    maritime = PythonOperator(
        task_id="collect_maritime",
        python_callable=collect_maritime_task,
    )

    cyber = PythonOperator(
        task_id="collect_cyber",
        python_callable=collect_cyber_task,
    )

    compute = PythonOperator(
        task_id="compute_threats",
        python_callable=compute_threats_task,
    )

    # Collectors run in parallel, then threat computation
    [diplomatic, notam, sns, telegram, maritime, cyber] >> compute

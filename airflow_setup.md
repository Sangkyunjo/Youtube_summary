# Airflow Setup Guide for YouTube Summary System

This guide explains how to set up Apache Airflow to run the YouTube Summary System.

## Option 1: Local Installation (Recommended for Development)

### Step 1: Create a Virtual Environment

```bash
cd C:\Users\trill\Youtube_summary
python -m venv airflow_venv
airflow_venv\Scripts\activate
```

### Step 2: Install Airflow

```bash
# Set Airflow home directory
set AIRFLOW_HOME=C:\Users\trill\Youtube_summary\airflow_home

# Install Airflow with constraints
pip install "apache-airflow==2.8.0" --constraint "https://raw.githubusercontent.com/apache/airflow/constraints-2.8.0/constraints-3.10.txt"

# Install project dependencies
pip install -r requirements.txt
```

### Step 3: Initialize Airflow Database

```bash
airflow db init
```

### Step 4: Create Admin User

```bash
airflow users create ^
    --username admin ^
    --password admin ^
    --firstname Admin ^
    --lastname User ^
    --role Admin ^
    --email admin@example.com
```

### Step 5: Configure Airflow

Edit `airflow_home/airflow.cfg`:

```ini
[core]
dags_folder = C:\Users\trill\Youtube_summary\dags
load_examples = False
executor = SequentialExecutor

[webserver]
web_server_port = 8080
```

### Step 6: Start Airflow

Open two terminal windows:

**Terminal 1 - Web Server:**
```bash
set AIRFLOW_HOME=C:\Users\trill\Youtube_summary\airflow_home
airflow webserver --port 8080
```

**Terminal 2 - Scheduler:**
```bash
set AIRFLOW_HOME=C:\Users\trill\Youtube_summary\airflow_home
airflow scheduler
```

### Step 7: Access Web UI

Open browser: http://localhost:8080

Login with admin/admin

---

## Option 2: Docker Compose (Recommended for Production)

### Step 1: Create docker-compose.yaml

See the `docker-compose.yaml` file in this directory.

### Step 2: Start Services

```bash
docker-compose up -d
```

### Step 3: Access Web UI

Open browser: http://localhost:8080

Default login: airflow/airflow

---

## DAG Configuration

The DAG is configured with:

| Setting | Value |
|---------|-------|
| Schedule | Daily at 11:00 PM (`0 23 * * *`) |
| Catchup | Enabled (backfills missed runs) |
| Max Active Runs | 1 (sequential execution) |
| Retries | 2 attempts with 5 min delay |
| Execution Timeout | 30 min (2 hours for processing) |

## Task Pipeline

```
start
  │
  ▼
check_channels ─── (skip if no channels)
  │
  ▼
get_new_videos
  │
  ▼
process_videos
  │
  ▼
send_email_report
  │
  ▼
cleanup_old_state
  │
  ▼
record_run_status
  │
  ▼
end
```

## Manual Trigger

To manually trigger the DAG:

1. Open Airflow Web UI
2. Find `youtube_summary_system` DAG
3. Click the "Play" button (trigger)
4. Optionally set execution date for backfill

Or via CLI:
```bash
airflow dags trigger youtube_summary_system
```

## Backfill Missed Runs

To backfill specific date range:
```bash
airflow dags backfill youtube_summary_system ^
    --start-date 2026-01-01 ^
    --end-date 2026-01-28
```

## Monitoring

### View Logs
- Web UI: Click on task instance → View Log
- CLI: `airflow tasks logs youtube_summary_system <task_id> <execution_date>`

### Check Task Status
```bash
airflow tasks state youtube_summary_system process_videos 2026-01-28
```

## Troubleshooting

### DAG Not Appearing
1. Check `dags_folder` in airflow.cfg
2. Verify no Python syntax errors: `python dags/youtube_summary_dag.py`
3. Restart scheduler

### Import Errors
Ensure project path is correct in DAG file:
```python
PROJECT_DIR = Path("C:/Users/trill/Youtube_summary")
```

### Permission Issues
Run Airflow with appropriate permissions to access:
- Project directory
- Config files (.env, config.yaml)
- Output directories (summaries, logs)

# YouTube Summary System

Monitors YouTube channels, extracts transcripts, and generates AI-powered summaries via email — automatically, every day.

## How It Works

1. Checks configured YouTube channels for new videos
2. Extracts transcripts (Korean preferred, English fallback)
3. Summarizes each video using OpenAI GPT
4. Saves summaries to a folder and sends an email digest

---

## Requirements

- Python 3.10+
- OpenAI API key → https://platform.openai.com/api-keys
- Gmail account with App Password (for email notifications)

---

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure API key and email

Edit `config/.env`:

```env
OPENAI_API_KEY=sk-...your key here...

EMAIL_SENDER=you@gmail.com
EMAIL_RECIPIENT=you@gmail.com
EMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx
```

To get a Gmail App Password: Google Account → Security → 2-Step Verification → App Passwords

### 3. Add YouTube channels

Edit `config/channels.yaml`:

```yaml
channels:
  - "@channelhandle"
  - "UCxxxxxxxxxxxxxxxxxxxxxx"   # or use a channel ID
```

### 4. Configure model and settings

Edit `config/config.yaml`:

```yaml
# Available models: gpt-4.1-mini, gpt-4.1, gpt-4o-mini, gpt-4o
openai:
  model: "gpt-4.1-mini"
  max_tokens: 4096

schedule:
  run_time: "23:00"         # Daily run time (24h)
  max_video_age_days: 7     # How far back to look for new videos

paths:
  summaries_dir: "D:/path/to/your/summaries/folder"
```

---

## Usage

### Run manually

```bash
cd src
python main.py
```

### Common options

```bash
python main.py --force          # Run even if already ran today
python main.py --skip-email     # Skip sending email
python main.py --dry-run        # Preview new videos without processing
python main.py --status         # Show system status
```

### Process historical videos

```bash
python main.py --historical \
  --channel @channelhandle \
  --start-date 2026-01-01 \
  --end-date 2026-01-31

# Preview before processing
python main.py --historical --channel @channelhandle \
  --start-date 2026-01-01 --end-date 2026-01-31 --dry-run

# Reprocess already-processed videos
python main.py --historical --channel @channelhandle \
  --start-date 2026-01-01 --end-date 2026-01-31 --reprocess
```

---

## Automated Scheduling

### Option A: Windows Task Scheduler (simple)

Create a task that runs daily:
```
Program: python
Arguments: C:\Users\trill\Youtube_summary\src\main.py --force
```

### Option B: Apache Airflow

See [airflow_setup.md](airflow_setup.md) for full setup instructions.

The DAG `youtube_summary_system` runs daily at 11:00 PM with automatic catchup for missed runs.

---

## Project Structure

```
Youtube_summary/
├── src/
│   ├── main.py               # Entry point & orchestrator
│   ├── channel_monitor.py    # Detects new YouTube videos via RSS
│   ├── transcript_extractor.py  # Downloads transcripts
│   ├── summarizer.py         # OpenAI GPT summarization
│   ├── email_notifier.py     # Gmail email digest
│   ├── scheduler.py          # Run scheduling logic
│   ├── state_manager.py      # Tracks processed videos
│   ├── historical_fetcher.py # Backfill / historical mode
│   └── utils.py              # Shared helpers
├── config/
│   ├── config.yaml           # Main configuration
│   ├── channels.yaml         # YouTube channels to monitor
│   └── .env                  # API keys and secrets (never commit!)
├── data/
│   ├── state.json            # Processed video tracking
│   └── last_run.json         # Last run metadata
├── logs/                     # Per-module log files
├── dags/                     # Airflow DAG
└── requirements.txt
```

---

## Summary Output Format

Each video produces a `.txt` file:

```
================================================================================
VIDEO SUMMARY
================================================================================
Title: ...
Channel: ...
Published: ...
URL: ...

OVERVIEW
KEY POINTS
NOTABLE QUOTES
KEY TAKEAWAYS
================================================================================
```

---

## Switching Models

To switch LLM models, edit `config/config.yaml`:

```yaml
openai:
  model: "gpt-4.1-mini"   # change this line
```

| Model | Speed | Cost | Quality |
|-------|-------|------|---------|
| gpt-4o-mini | Fastest | Lowest | Good |
| gpt-4.1-mini | Fast | Low | Better |
| gpt-4o | Medium | Medium | High |
| gpt-4.1 | Medium | Highest | Best |

---

## Changelog

All changes made via Claude Code are automatically logged in [`CHANGELOG.md`](CHANGELOG.md). Each entry records the timestamp, action type (Created/Edited/Committed), and the affected file.

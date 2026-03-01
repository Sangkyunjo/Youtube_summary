# CLAUDE.md — Project Notes for Claude Code

## Project Overview

YouTube channel monitor that extracts transcripts and summarizes videos using the OpenAI API, then emails a daily digest.

**LLM:** OpenAI GPT (currently `gpt-4.1-mini`) — configured in `config/config.yaml`
**API key env var:** `OPENAI_API_KEY` in `config/.env`

---

## Key Files

| File | Role |
|------|------|
| `src/main.py` | Entry point, CLI args, orchestrates all components |
| `src/summarizer.py` | OpenAI API calls — the main LLM integration |
| `src/channel_monitor.py` | Polls YouTube RSS feeds for new videos |
| `src/transcript_extractor.py` | Downloads transcripts via youtube-transcript-api |
| `src/email_notifier.py` | Sends Gmail digest with summaries |
| `src/scheduler.py` | Decides whether to run based on last run time |
| `src/state_manager.py` | Persists processed video IDs in `data/state.json` |
| `src/historical_fetcher.py` | Fetches videos by date range for backfill |
| `src/utils.py` | Shared helpers: config loading, logging, filename generation |
| `config/config.yaml` | Main config: model, schedule, paths, email settings |
| `config/channels.yaml` | List of YouTube channels to monitor |
| `config/.env` | Secrets: OPENAI_API_KEY, email credentials |
| `dags/youtube_summary_dag.py` | Airflow DAG (alternative to manual scheduling) |

---

## Architecture

```
ChannelMonitor → new VideoInfo list
  → TranscriptExtractor → transcript text
    → Summarizer (OpenAI) → sections dict
      → format + save .txt file
        → EmailNotifier → Gmail digest
          → StateManager → mark as processed
```

All components are initialized in `YouTubeSummarySystem.__init__()` in `main.py`. The `Summarizer` is lazy-initialized on first use.

---

## LLM Integration (summarizer.py)

- Uses `openai.OpenAI` client
- Model configured via `config/config.yaml` under `openai.model`
- API key read from env var `OPENAI_API_KEY`
- Transcripts > 100,000 chars are truncated before sending
- Response is parsed into 4 sections: overview, key_points, quotes, takeaways
- To change the model: edit `config/config.yaml` → `openai.model`

Available models: `gpt-4.1-mini`, `gpt-4.1`, `gpt-4o-mini`, `gpt-4o`

---

## Running the System

```bash
cd src
python main.py              # normal daily run
python main.py --force      # force run
python main.py --dry-run    # preview only
python main.py --status     # check status
```

Historical backfill:
```bash
python main.py --historical --channel @handle --start-date YYYY-MM-DD --end-date YYYY-MM-DD
```

---

## Config Locations

- Model selection: `config/config.yaml` → `openai.model`
- Channels: `config/channels.yaml` → `channels:` list
- Summary output path: `config/config.yaml` → `paths.summaries_dir`
- Schedule time: `config/config.yaml` → `schedule.run_time`

---

## State & Deduplication

Processed video IDs are stored in `data/state.json`. The system skips any video already in this file. Old entries are cleaned up after 90 days (`cleanup_old_entries(days=90)`).

---

## Logging

Each module writes to its own log file in `logs/`:
- `logs/main.log`, `logs/summarizer.log`, `logs/email_notifier.log`, etc.
- Log level and rotation configured in `config/config.yaml` → `logging`

---

## Dependencies

```
openai>=1.0.0
youtube-transcript-api
feedparser
yt-dlp
pyyaml
python-dotenv
requests
python-dateutil
```

---

## Important Notes

- `config/.env` contains secrets — ensure it is in `.gitignore`
- The system runs once per day by default; `--force` bypasses this check
- Transcripts are fetched in Korean first, then English fallback (`config/config.yaml` → `transcript.languages`)
- Channel handles (`@name`) are resolved to IDs automatically via yt-dlp

---

## Changelog System

An auto-updating `CHANGELOG.md` is maintained via a Claude Code PostToolUse hook.

**How it works:**
- A bash hook (`.claude/hooks/update-changelog.sh`) fires after every `Write`, `Edit`, or `Bash` tool use
- For `Write`/`Edit`: logs the file path and action type
- For `Bash`: only logs `git commit` commands (all other commands are ignored)
- Entries are appended to `CHANGELOG.md` with a timestamp

**Config locations:**
- Hook script: `.claude/hooks/update-changelog.sh`
- Hook config: `.claude/settings.local.json` → `hooks.PostToolUse`

**Anti-recursion skip patterns:** `CHANGELOG.md`, `.claude/*`, `logs/*`, `data/state.json`, `data/last_run.json`, `__pycache__`, temp files (`.tmp`, `.pyc`)

**To disable:** Remove the `hooks` key from `.claude/settings.local.json`

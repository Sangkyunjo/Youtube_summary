# YouTube Summary System

Monitors YouTube channels, extracts transcripts, and generates AI-powered summaries via email — automatically, every day.

## How It Works

1. Checks configured YouTube channels for new videos
2. Extracts transcripts (Korean preferred, English fallback)
3. Summarizes each video using Qwen through Alibaba DashScope
4. Saves summaries to a folder and sends an email digest

---

## Requirements

- Python 3.10+
- Alibaba DashScope Qwen API key
- Gmail account with App Password (for email notifications)
- A rotating residential proxy only when YouTube blocks public-caption requests

---

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure API key and email

Edit `config/.env`:

```env
QWEN_API_KEY=...your key here...

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
# Available default: qwen-plus
llm:
  provider: "qwen"

qwen:
  model: "qwen-plus"
  max_tokens: 4096
  base_url: "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"

schedule:
  run_time: "23:00"         # Daily run time (24h)
  max_video_age_days: 7     # How far back to look for new videos

paths:
  summaries_dir: "D:/path/to/your/summaries/folder"
```

### 5. NarrativeRadar queue mode and caption proxy

The production queue path is captions-first and summarizes with
`qwen-plus`. Whisper is constructed only when YouTube confirms that a video
has no usable caption track or the public-caption endpoint returns `IpBlocked`.
That auxiliary path downloads a compact media resource directly, then passes
only its audio to transcription after the file is complete. YouTube's separately
served ads and playback buffering therefore do not enter the transcript. A
generic error or unavailable video does not trigger paid STT. Creator-embedded
sponsorship is left intact because it is part of the uploaded source content.

Keep proxy credentials out of `config/config.yaml`. Set the single proxy URL
in TrillionStack's gitignored `.env`; it is used for both HTTP and HTTPS
caption requests:

```env
YOUTUBE_TRANSCRIPT_PROXY_URL=http://username:password@proxy-host:port
```

After changing the compose environment, recreate only the Airflow scheduler:

```bash
cd /Users/sangkyun/TrillionStack
docker compose -f docker-compose.infra.yml -f docker-compose.airflow.yml \
  up -d --force-recreate airflow-scheduler
```

Then run one read-only caption probe before resuming the queue:

```bash
docker exec -w /opt/airflow/src devenv-airflow-scheduler \
  python -c "from transcript_extractor import TranscriptExtractor; print(TranscriptExtractor().extract_with_status('KOgT1xnG_nw')[1])"
```

Expected status is `ok`, `no_captions`, or `unavailable`. A caption-only probe
can still return `blocked`; production queue mode then tries the independent
direct-media auxiliary path before treating the video as blocked.

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
│   ├── summarizer.py         # Qwen summary via an OpenAI-compatible client
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

## PersonalAgent realtime fallback (2026-09-06)

`realtime_fallback.enabled: true` now routes caption-blocked / caption-missing
videos to a persistent Mac playback queue. This applies to daily/historical
runs and NarrativeRadar's `captions-first` mode. In this configuration,
`captions-first` uses official visible UI captions followed by supervised local
playback instead of the older direct-media Whisper API fallback described above.
Explicit `captions` and `whisper` modes
retain their legacy meanings.

Queue mode has no daily cap and works newest-published first, so the requests a run
gets before a caption block go to fresh videos. A caption block ends the run and the
remaining rows stay queued for the next one. Each run drops waiting playback jobs for videos past
`NARRATIVES_YOUTUBE_MAX_AGE_DAYS`; transcripts already captured are still delivered.

Waiting jobs retry captions on every run, and a success closes the job without
playback. `--resume-realtime` does the same for daily-destination jobs, and a
`--queue` run without `--limit` calls it after the queue.

```bash
# Discovery and caption summaries; deferred work remains pending, no email.
docker exec -w /opt/airflow/src devenv-airflow-scheduler python main.py --force --skip-email

# Mac host: inspect deferred work and prerequisites.
python3 src/realtime_worker.py list
python3 src/realtime_worker.py check
python3 src/realtime_worker.py show VIDEO_ID

# With browser supervision, recording ready, and actual playback observations:
python3 src/realtime_worker.py record VIDEO_ID --seconds MAX_CAPTURE_SECONDS
python3 src/realtime_worker.py observe VIDEO_ID --file /absolute/path/player-observation.json

# After a verified local transcript is ready; never sends email.
docker exec -w /opt/airflow/src devenv-airflow-scheduler python main.py --resume-realtime
```

The worker reuses `fanding-kb/bin/fkb-rec` and the installed whisper.cpp model.
It verifies sampled player identity, timing, 1x speed, ads, mute/buffering and
actual end, then trims startup/tail audio before transcription. An ended
observation gracefully stops recording; the seconds argument is an upper bound.
No hidden media extraction or transcription API is used in this path.

`data/realtime.sqlite3` is shared across host and container. `pending`/`failed`
require browser playback; `ready` means local transcription succeeded.
`pending_outputs=0` means all registered summary destinations were delivered.
The recorder shares one kernel lock with Fanding and ValleyAI captures.
Interrupted jobs stay retryable. `recover VIDEO_ID` resets a stale recording
only when no capture owns the lock. Temporary audio is removed; transcripts
remain in the local, Git-ignored database for resume.

This is a **browser-supervised worker**, not an unattended browser daemon.
Use the personal `summarize-videos` skill for the complete operating procedure,
audio setup/restore, and course-specific routing. Existing Airflow schedules
are unchanged. No email or publishing is implied by playback or resume.

## Daily playback selection bot (2026-11-05)

The caption endpoint blocks this host after about eleven requests, so a host-side
bot offers real-time playback instead. It reuses the supervised worker above but
can replace the human observer with an automated one when explicitly enabled.

- **08:00 KST offer.** The conversation bot `@trillionnt_bot` (PersonalAgent
  `TELEGRAM_HERMES_*` values) sends the newest 30 unprocessed in-window queue videos
  with their length, from `main.py --candidates 30` in the scheduler container.
  Videos left over from the previous day come first.
- **Selection.** Reply with numbers (`1,3,5-8`), `전체` or `없음`. `상태`, `목록` and
  `중단` also work, including while a video plays.
- **Recovery (current default).** Chosen videos first try the official visible
  transcript panel in a dedicated Chrome profile. Complete captures enter the
  shared queue directly; no recording or full playback is needed. Missing/loading
  panels become supervised playback jobs, reported separately from failures.
- **Optional automated playback.** With `transcript_fallback.automated_playback: true`, videos play one at a time in a dedicated Chrome profile
  (`data/chrome-profile`, Playwright driving the installed Chrome) while
  `realtime_worker.py record --cut-interruptions` records system audio. Ads, stalls
  and unexpected pauses are cut from the audio (`playback_segments`); seeks, speed
  changes, muted content and wrong videos still reject the recording. A bot check
  or consent wall stops the session and is reported, never worked around.
- **Outputs.** Each verified transcript is summarized into the NarrativeRadar inbox
  and `summaries/` (`main.py --deliver-ready VIDEO_ID`).
- **Limits.** After every three hours of processing the bot asks `계속` or `중단`.
  Playback runs only 09:00–23:00 KST, and a video that cannot finish before 23:00
  waits for the next offer.

Service files: `launchd/com.sangkyun.youtube-summary.playback-bot.plist` (symlinked
into `~/Library/LaunchAgents`), host environment `.venv-host` (Python 3.12,
Playwright, PyYAML), logs `logs/playback_bot*.log`, state `data/playback_bot.sqlite3`.

```bash
launchctl kickstart -k gui/$(id -u)/com.sangkyun.youtube-summary.playback-bot
launchctl bootout gui/$(id -u)/com.sangkyun.youtube-summary.playback-bot
```

## Transcript recovery pipeline (2026-11-08)

The default order is **caption API → official visible transcript → regular-browser
playback + local whisper.cpp → Qwen summary**. Failed UI capture no longer starts
automated Chrome recording by default. `transcript_fallback.automated_playback`
preserves that explicit option; generic player errors are reported without
asserting that YouTube detected automation. No new service is installed.

`data/realtime.sqlite3` retains existing jobs and adds `next_action` and
`acquisition` fields with an additive migration. Stages are `ui_transcript`,
`playback` and `deliver`; source types distinguish API captions, visible UI
captions and local ASR. Recording status and destination completion remain separate.
Old caption jobs without acquisition metadata retain an explicitly legacy source
with unknown language/generation rather than guessing an API/UI route.
Ready transcripts can be delivered even after their discovery file disappears.

The host bot uses `browser_transcript.VisibleTranscriptReader` to click the
official transcript controls and read rendered rows. It does not read hidden
caption URLs or intercept requests. A loading, partial or changed-video panel is
left retryable. The UI adapter depends on YouTube's rendered controls; a regular
Safari/Chrome session can supply the same input through the CLI:

```bash
.venv-host/bin/python src/realtime_worker.py list
.venv-host/bin/python src/realtime_worker.py show VIDEO_ID
.venv-host/bin/python src/realtime_worker.py import-ui VIDEO_ID --file /absolute/path/ui-capture.json
docker exec -w /opt/airflow/src devenv-airflow-scheduler python main.py --deliver-ready VIDEO_ID

# If the official panel cannot be read, use the existing supervised recorder.
.venv-host/bin/python src/realtime_worker.py ui-unavailable VIDEO_ID --reason ui_loading
# Verify actual audio, rewind, then record and submit fresh playback observations.
.venv-host/bin/python src/realtime_worker.py record VIDEO_ID --seconds MAX_CAPTURE_SECONDS

# Explicitly retry a previously unavailable transcript panel.
.venv-host/bin/python src/realtime_worker.py retry-ui VIDEO_ID
```

UI capture JSON must identify the actual video and assert that the panel was read
to its end. Keep the UI's timestamps and original text; do not invent durations:

```json
{
  "video_id": "ACTUAL_ID11",
  "source": "youtube_visible_transcript",
  "captured_at": 1794135300.178,
  "complete": true,
  "language": "ko",
  "generated": null,
  "segments": [
    {"start_seconds": 0, "text": "First caption, with the actual complete source text..."},
    {"start_seconds": 129, "text": "Last actual caption..."}
  ]
}
```

The example is schematic: imports require at least 200 characters and a valid
capture timestamp. Imports cannot overwrite an active recording or ready input.
This command only stores the transcript; `--deliver-ready` writes the registered
summary destinations without email. Existing Qwen and OneDrive settings remain.

Each new daily summary has a `.meta.json` sidecar containing input provenance,
transcript SHA-256, original caption segments when available and review status.
NarrativeRadar carries the same provenance/review information in its contract.
Automatic summaries are labelled **검수 상태: 확인 대기**. Quote mismatches and
input truncation are flagged; names, numbers and speaker attribution still need
review. Summary saving and verified accuracy are distinct stages.

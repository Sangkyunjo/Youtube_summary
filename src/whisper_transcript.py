"""Whisper-based transcript backend for the NarrativeRadar queue.

Downloads a compact audio-only stream and transcribes it with the OpenAI
Whisper API. Unlike youtube-transcript-api (which reads YouTube's caption
endpoint and gets IP-rate-limited on bursts), this never touches the caption
endpoint — so it sidesteps that block. It does still fetch audio from YouTube
(a different, more tolerant subsystem), throttled by the caller.

Design:
  - Download a single low-bitrate audio format directly (no post-processing /
    no merge). ~48 kbps m4a ≈ 0.36 MB/min, so a ~70-minute video stays under
    Whisper's 25 MB upload limit and is sent as-is.
  - Files over the limit are re-encoded to 16 kHz mono and split into
    time-based chunks with ffmpeg (when ffmpeg is on PATH); each chunk is
    transcribed and the texts are concatenated. If ffmpeg is unavailable the
    oversized file is skipped (logged loudly).

Exposes ``extract_with_status(video_id) -> (text, status)`` matching
TranscriptExtractor so the queue consumer can swap backends transparently.
Statuses: "ok" | "no_captions" (no/!usable audio — permanent) |
"blocked" (YouTube 429/forbidden — abort & retry later) | "error" (transient).
"""

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

import openai

from utils import get_env, load_config, setup_logging

logger = setup_logging("whisper_transcript")

# OpenAI Whisper hard limit is 25 MB; stay just under it.
_MAX_UPLOAD_MB = float(os.getenv("NARRATIVES_WHISPER_MAX_MB", "24"))
# ffmpeg is used to re-encode + chunk oversized audio. Honour an explicit path
# override, else look it up on PATH. None → chunking disabled (skip oversized).
_FFMPEG = os.getenv("NARRATIVES_FFMPEG") or shutil.which("ffmpeg")
# Chunk length (seconds) for oversized audio. At 32 kbps mono (~0.24 MB/min) a
# 15-minute chunk is ~3.6 MB — comfortably under the upload limit for any length.
_CHUNK_SECONDS = int(os.getenv("NARRATIVES_WHISPER_CHUNK_SECONDS", "900"))
_CHUNK_BITRATE = os.getenv("NARRATIVES_WHISPER_CHUNK_BITRATE", "32k")
# Hard cap on the re-encode/segment step so a corrupt file can't hang the run.
_FFMPEG_TIMEOUT = int(os.getenv("NARRATIVES_FFMPEG_TIMEOUT", "600"))
# Prefer the smallest audio-only stream (lowest bitrate) to stay under the limit
# without transcoding. Falls back to bestaudio/best if no tiny format exists.
_AUDIO_FORMAT = os.getenv("NARRATIVES_WHISPER_AUDIO_FORMAT", "worstaudio/bestaudio/best")

# YouTube now requires authentication for downloads. Easiest: let yt-dlp read
# cookies straight from an installed, YouTube-logged-in browser — set
# NARRATIVES_YOUTUBE_COOKIES_FROM_BROWSER=chrome|edge|firefox|brave. Falls back
# to a Netscape cookies.txt via the transcript.cookie_file config.
_COOKIES_FROM_BROWSER = (os.getenv("NARRATIVES_YOUTUBE_COOKIES_FROM_BROWSER") or "").strip()


class WhisperTranscriber:
    """Audio-download + OpenAI Whisper transcription backend."""

    def __init__(self):
        config = load_config()
        wcfg = config.get("whisper", {}) or {}
        self.model = os.getenv("NARRATIVES_WHISPER_MODEL") or wcfg.get("model", "whisper-1")
        self.engine = "whisper"

        api_key = (get_env("OPENAI_API_KEY") or "").strip()
        if not api_key or api_key.startswith("your_"):
            raise ValueError("OPENAI_API_KEY not configured (config/.env) — required for Whisper")
        # Force the real OpenAI endpoint (the Summarizer may point at Qwen/MiniMax).
        self.client = openai.OpenAI(api_key=api_key)

        # Cookie file for the authenticated download YouTube now requires.
        # Precedence: NARRATIVES_YOUTUBE_COOKIE_FILE env > transcript.cookie_file
        # in config.yaml (default config/youtube_cookies.txt). The env override
        # lets the cookie live outside the (synced) repo without editing config.
        tcfg = config.get("transcript", {}) or {}
        cookie_path = os.getenv("NARRATIVES_YOUTUBE_COOKIE_FILE") or tcfg.get("cookie_file")
        self.cookie_file = self._resolve_cookie(cookie_path)
        logger.info("WhisperTranscriber ready (model=%s)", self.model)

    @staticmethod
    def _resolve_cookie(cookie_file: Optional[str]) -> Optional[str]:
        if not cookie_file:
            return None
        if not os.path.isabs(cookie_file):
            root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            cookie_file = os.path.join(root, cookie_file)
        return cookie_file if os.path.exists(cookie_file) else None

    def _download_audio(self, video_id: str, dest_dir: str) -> tuple[Optional[Path], str]:
        """Download a compact audio file. Returns (path, status)."""
        from yt_dlp import YoutubeDL

        opts = {
            "format": _AUDIO_FORMAT,
            "outtmpl": os.path.join(dest_dir, "%(id)s.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            # The default `web` client now needs PO tokens and 403s on media URLs,
            # and `web`/`ios`/`tv` now hand back DRM-protected streams for many
            # videos → "Requested format is not available". The `android` client
            # still serves downloadable, non-DRM formats unauthenticated, so try it
            # first and keep the others as fallbacks.
            "extractor_args": {"youtube": {"player_client": ["android", "web", "ios", "tv"]}},
            "retries": 3,
        }
        if _COOKIES_FROM_BROWSER:
            opts["cookiesfrombrowser"] = (_COOKIES_FROM_BROWSER,)
        elif self.cookie_file:
            opts["cookiefile"] = self.cookie_file
        try:
            with YoutubeDL(opts) as ydl:
                ydl.download([f"https://youtu.be/{video_id}"])
        except Exception as e:  # noqa: BLE001
            msg = str(e).lower()
            if any(s in msg for s in ("429", "too many requests", "rate limit", "forbidden", "sign in to confirm")):
                logger.warning("whisper: audio download blocked for %s (%s)", video_id, type(e).__name__)
                return None, "blocked"
            if any(s in msg for s in ("unavailable", "private", "removed", "members-only", "no video formats")):
                logger.info("whisper: %s unavailable / no audio", video_id)
                return None, "no_captions"
            logger.warning("whisper: audio download error for %s: %s", video_id, e)
            return None, "error"

        files = sorted(Path(dest_dir).glob(f"{video_id}.*"))
        if not files:
            return None, "error"
        return files[0], "ok"

    def _transcribe_file(self, path: Path) -> tuple[Optional[str], str]:
        """Send a single (under-limit) audio file to Whisper. Returns (text, status)."""
        try:
            with open(path, "rb") as fh:
                resp = self.client.audio.transcriptions.create(model=self.model, file=fh)
        except openai.APIError as e:
            logger.error("whisper: OpenAI API error for %s: %s", path.name, e)
            return None, "error"
        text = (getattr(resp, "text", "") or "").strip()
        return (text, "ok") if text else (None, "no_captions")

    def _chunk_with_ffmpeg(self, audio: Path, dest_dir: str) -> list[Path]:
        """Re-encode to 16 kHz mono and split into time-based chunks. Returns sorted parts."""
        out_tmpl = os.path.join(dest_dir, "chunk_%04d.mp3")
        cmd = [
            _FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(audio),
            "-vn", "-ac", "1", "-ar", "16000", "-b:a", _CHUNK_BITRATE,
            "-f", "segment", "-segment_time", str(_CHUNK_SECONDS),
            out_tmpl,
        ]
        subprocess.run(
            cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            timeout=_FFMPEG_TIMEOUT,
        )
        return sorted(Path(dest_dir).glob("chunk_*.mp3"))

    def _transcribe_large(self, video_id: str, audio: Path, tmpdir: str) -> tuple[Optional[str], str]:
        """Chunk an oversized file with ffmpeg and transcribe each part."""
        chunk_dir = tempfile.mkdtemp(prefix="ytwchunks_", dir=tmpdir)
        try:
            chunks = self._chunk_with_ffmpeg(audio, chunk_dir)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
            stderr = getattr(e, "stderr", b"")
            detail = stderr.decode("utf-8", "ignore").strip() if isinstance(stderr, bytes) else ""
            logger.warning("whisper: ffmpeg chunking failed for %s: %s %s", video_id, e, detail)
            return None, "error"

        if not chunks:
            logger.warning("whisper: ffmpeg produced no chunks for %s", video_id)
            return None, "error"

        logger.info("whisper: transcribing %s in %d chunks (model=%s)", video_id, len(chunks), self.model)
        parts: list[str] = []
        for idx, chunk in enumerate(chunks, 1):
            text, status = self._transcribe_file(chunk)
            if status == "error":
                logger.warning("whisper: chunk %d/%d failed for %s — aborting", idx, len(chunks), video_id)
                return None, "error"
            if text:
                parts.append(text)
        joined = " ".join(parts).strip()
        return (joined, "ok") if joined else (None, "no_captions")

    def extract_with_status(self, video_id: str) -> tuple[Optional[str], str]:
        tmpdir = tempfile.mkdtemp(prefix="ytwhisper_")
        try:
            audio, status = self._download_audio(video_id, tmpdir)
            if status != "ok" or audio is None:
                return None, status

            size_mb = audio.stat().st_size / 1_000_000
            if size_mb > _MAX_UPLOAD_MB:
                if not _FFMPEG:
                    # No ffmpeg to compress/chunk — skip permanently rather than loop.
                    logger.warning(
                        "whisper: %s audio %.1fMB exceeds %.0fMB limit — skipping (install ffmpeg to chunk)",
                        video_id, size_mb, _MAX_UPLOAD_MB,
                    )
                    return None, "no_captions"
                logger.info(
                    "whisper: %s audio %.1fMB exceeds %.0fMB limit — chunking with ffmpeg",
                    video_id, size_mb, _MAX_UPLOAD_MB,
                )
                return self._transcribe_large(video_id, audio, tmpdir)

            logger.info("whisper: transcribing %s (%.1fMB, model=%s)", video_id, size_mb, self.model)
            return self._transcribe_file(audio)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

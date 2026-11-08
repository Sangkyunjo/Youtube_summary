"""
Transcript extraction module for YouTube videos.
Uses youtube-transcript-api with Korean first, English fallback.
Supports optional cookies and proxy configuration.
"""

import http.cookiejar
import os
import re
from typing import Optional

import requests
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import (
    CouldNotRetrieveTranscript,
    IpBlocked,
    NoTranscriptFound,
    RequestBlocked,
    TranscriptsDisabled,
    VideoUnavailable,
)
from youtube_transcript_api.proxies import GenericProxyConfig

from utils import get_env, load_config, setup_logging

logger = setup_logging("transcript_extractor")


class TranscriptExtractor:
    """Extracts transcripts from YouTube videos."""

    def __init__(self):
        self.engine = "youtube-transcript-api"
        self.model = None
        self.last_track_info = None
        self.last_segments = None
        self.config = load_config()
        transcript_config = self.config.get("transcript", {})
        self.preferred_languages = transcript_config.get("languages", ["ko", "en"])
        self.fallback_to_any = transcript_config.get("fallback_to_any", True)
        self.min_transcript_chars = int(os.getenv(
            "NARRATIVES_YOUTUBE_MIN_TRANSCRIPT_CHARS",
            transcript_config.get("min_chars", 200),
        ))

        # Build API client with optional cookie/proxy support
        proxy_config = self._build_proxy_config(transcript_config)
        http_client = self._build_http_client(transcript_config)
        self.api = YouTubeTranscriptApi(
            proxy_config=proxy_config,
            http_client=http_client,
        )

    def _build_proxy_config(self, transcript_config: dict) -> Optional[GenericProxyConfig]:
        """Build proxy config, preferring the secret-bearing environment value."""
        proxy = transcript_config.get("proxy") or {}
        env_url = (get_env("YOUTUBE_TRANSCRIPT_PROXY_URL") or "").strip()
        http_url = env_url or (proxy.get("http_url") or "").strip() or None
        https_url = env_url or (proxy.get("https_url") or "").strip() or None
        for url in (http_url, https_url):
            if url and not url.startswith(("http://", "https://")):
                raise ValueError(
                    "YouTube transcript proxy URL must start with http:// or https://"
                )
        if http_url or https_url:
            logger.info("Using configured proxy for transcript requests")
            return GenericProxyConfig(http_url=http_url, https_url=https_url)
        return None

    def _build_http_client(self, transcript_config: dict) -> Optional[requests.Session]:
        """Build requests.Session with cookies if a cookie file is configured."""
        cookie_file = transcript_config.get("cookie_file")
        if cookie_file:
            # Resolve relative paths from project root
            if not os.path.isabs(cookie_file):
                project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                cookie_file = os.path.join(project_root, cookie_file)
            if os.path.exists(cookie_file):
                logger.info("Loading YouTube cookies from configured cookie file")
                try:
                    session = requests.Session()
                    cookie_jar = http.cookiejar.MozillaCookieJar(cookie_file)
                    cookie_jar.load(ignore_discard=True, ignore_expires=True)
                    session.cookies = cookie_jar
                    return session
                except (http.cookiejar.LoadError, OSError, PermissionError) as e:
                    logger.warning(f"Failed to load cookie file, proceeding without cookies: {e}")
            else:
                logger.warning("Configured cookie file not found, proceeding without cookies")
        return None

    def extract(self, video_id: str) -> Optional[str]:
        """Extract transcript text, or None if unavailable (any reason)."""
        return self.extract_with_status(video_id)[0]

    def extract_with_status(self, video_id: str) -> tuple[Optional[str], str]:
        """Extract transcript and classify the outcome.

        Returns ``(text, status)`` where status is one of:
          - ``"ok"``          : transcript text returned
          - ``"no_captions"`` : video genuinely has no usable transcript
                                (disabled / none found / all tracks too short)
          - ``"unavailable"`` : video is private, removed, or otherwise unplayable
          - ``"blocked"``     : YouTube rate-limited / IP-blocked us — TRANSIENT,
                                the caller should back off, NOT mark the video done
          - ``"error"``       : other transient failure — caller may retry later

        Distinguishing ``blocked`` from ``no_captions`` matters: a caller that
        marks "no transcript" as permanently processed would otherwise silently
        drop every video hit during an IP block.
        """
        logger.info(f"Extracting transcript for video: {video_id}")
        self.last_track_info = None
        self.last_segments = None

        # Block detection must come before the generic CouldNotRetrieveTranscript
        # handler, since IpBlocked/RequestBlocked subclass it.
        try:
            tracks = list(self.api.list(video_id))
            ordered = []
            for lang in self.preferred_languages:
                ordered.extend(t for t in tracks if t.language_code == lang)
            if self.fallback_to_any:
                ordered.extend(t for t in tracks if t not in ordered)

            if not ordered:
                logger.warning(f"No transcript available for {video_id}")
                return None, "no_captions"

            fetch_errors = 0
            for transcript in ordered:
                try:
                    transcript_data = list(transcript.fetch())
                except (IpBlocked, RequestBlocked):
                    raise
                except CouldNotRetrieveTranscript as e:
                    fetch_errors += 1
                    logger.warning(
                        "Caption track fetch failed for %s (%s/%s, %s): %s",
                        video_id,
                        transcript.language_code,
                        "generated" if transcript.is_generated else "manual",
                        type(e).__name__,
                        e,
                    )
                    continue

                full_text = self._clean_transcript(
                    " ".join(entry.text for entry in transcript_data)
                )
                if len(full_text) < self.min_transcript_chars:
                    logger.warning(
                        "Ignoring too-short caption track for %s (%s/%s): %d chars",
                        video_id,
                        transcript.language_code,
                        "generated" if transcript.is_generated else "manual",
                        len(full_text),
                    )
                    continue

                self.last_track_info = {
                    "language": transcript.language_code,
                    "generated": bool(transcript.is_generated),
                }
                # Keep the original caption text and timing separately from the
                # cleaned summary input. Missing timing must never be invented.
                entries = list(transcript_data)
                if all(hasattr(e, "start") and hasattr(e, "duration") for e in entries):
                    self.last_segments = [
                        {"text": e.text, "start": float(e.start), "duration": float(e.duration)}
                        for e in entries
                    ]
                logger.info(
                    "Extracted transcript for %s: %d characters (%s/%s)",
                    video_id,
                    len(full_text),
                    transcript.language_code,
                    "generated" if transcript.is_generated else "manual",
                )
                return full_text, "ok"

            # Any failed track means we cannot prove that the video has no usable
            # captions. Treat the outcome as transient so captions-first never
            # turns a partial YouTube failure into a paid Whisper call.
            if fetch_errors:
                return None, "error"
            logger.warning(f"No usable transcript available for {video_id}")
            return None, "no_captions"

        except (IpBlocked, RequestBlocked) as e:
            logger.warning(f"YouTube blocked transcript request for {video_id} ({type(e).__name__})")
            return None, "blocked"
        except TranscriptsDisabled:
            logger.warning(f"Transcripts are disabled for video: {video_id}")
            return None, "no_captions"
        except VideoUnavailable:
            logger.warning(f"Video unavailable: {video_id}")
            return None, "unavailable"
        except NoTranscriptFound:
            logger.warning(f"No transcript found: {video_id}")
            return None, "no_captions"
        except CouldNotRetrieveTranscript as e:
            logger.warning(f"Could not retrieve transcript for video {video_id} ({type(e).__name__}): {e}")
            return None, "error"
        except Exception as e:
            logger.error(f"Error extracting transcript for {video_id} ({type(e).__name__}): {e}")
            return None, "error"

    def _clean_transcript(self, text: str) -> str:
        """
        Clean up transcript text.

        Args:
            text: Raw transcript text

        Returns:
            Cleaned transcript text
        """
        # Remove excessive whitespace
        text = re.sub(r'\s+', ' ', text)

        # Remove common artifacts
        text = text.replace('[Music]', '')
        text = text.replace('[Applause]', '')
        text = text.replace('[Laughter]', '')

        # Remove multiple spaces
        text = re.sub(r' +', ' ', text)

        return text.strip()

    def get_transcript_info(self, video_id: str) -> dict:
        """
        Get information about available transcripts for a video.

        Args:
            video_id: YouTube video ID

        Returns:
            Dictionary with transcript availability info
        """
        try:
            transcript_list = self.api.list(video_id)

            available = []
            for transcript in transcript_list:
                available.append({
                    "language": transcript.language,
                    "language_code": transcript.language_code,
                    "is_generated": transcript.is_generated,
                })

            return {
                "available": True,
                "transcripts": available
            }

        except (TranscriptsDisabled, CouldNotRetrieveTranscript):
            return {"available": False, "reason": "disabled_or_unavailable"}
        except VideoUnavailable:
            return {"available": False, "reason": "video_unavailable"}
        except Exception as e:
            return {"available": False, "reason": str(e)}


def main():
    """Test the transcript extractor."""
    extractor = TranscriptExtractor()

    # Test with a sample video ID
    test_video_id = input("Enter a YouTube video ID to test: ").strip()
    if test_video_id:
        print(f"\nGetting transcript info for: {test_video_id}")
        info = extractor.get_transcript_info(test_video_id)
        print(f"Info: {info}")

        print(f"\nExtracting transcript...")
        transcript = extractor.extract(test_video_id)
        if transcript:
            print(f"Transcript length: {len(transcript)} characters")
            print(f"First 500 characters:\n{transcript[:500]}...")
        else:
            print("No transcript available")


if __name__ == "__main__":
    main()

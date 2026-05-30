"""
Transcript extraction module for YouTube videos.
Uses youtube-transcript-api with Korean first, English fallback.
Supports cookies (to bypass IP bans) and proxy configuration.
"""

import http.cookiejar
import os
import re
from typing import Optional

import requests
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api.proxies import GenericProxyConfig
from youtube_transcript_api._errors import (
    TranscriptsDisabled,
    NoTranscriptFound,
    VideoUnavailable,
    CouldNotRetrieveTranscript,
    IpBlocked,
    RequestBlocked,
)

from utils import load_config, setup_logging

logger = setup_logging("transcript_extractor")


class TranscriptExtractor:
    """Extracts transcripts from YouTube videos."""

    def __init__(self):
        self.config = load_config()
        transcript_config = self.config.get("transcript", {})
        self.preferred_languages = transcript_config.get("languages", ["ko", "en"])
        self.fallback_to_any = transcript_config.get("fallback_to_any", True)

        # Build API client with optional cookie/proxy support
        proxy_config = self._build_proxy_config(transcript_config)
        http_client = self._build_http_client(transcript_config)
        self.api = YouTubeTranscriptApi(
            proxy_config=proxy_config,
            http_client=http_client,
        )

    def _build_proxy_config(self, transcript_config: dict) -> Optional[GenericProxyConfig]:
        """Build proxy config from config.yaml settings."""
        proxy = transcript_config.get("proxy", {})
        http_url = proxy.get("http_url")
        https_url = proxy.get("https_url")
        if http_url or https_url:
            logger.info("Using proxy for transcript requests")
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
                                (disabled / none found / unavailable) — permanent
          - ``"blocked"``     : YouTube rate-limited / IP-blocked us — TRANSIENT,
                                the caller should back off, NOT mark the video done
          - ``"error"``       : other transient failure — caller may retry later

        Distinguishing ``blocked`` from ``no_captions`` matters: a caller that
        marks "no transcript" as permanently processed would otherwise silently
        drop every video hit during an IP block.
        """
        logger.info(f"Extracting transcript for video: {video_id}")

        # Block detection must come before the generic CouldNotRetrieveTranscript
        # handler, since IpBlocked/RequestBlocked subclass it.
        try:
            # Get list of available transcripts
            transcript_list = self.api.list(video_id)

            transcript = None

            # Try to find a transcript in preferred languages
            for lang in self.preferred_languages:
                try:
                    transcript = transcript_list.find_transcript([lang])
                    logger.info(f"Found {lang} transcript for {video_id}")
                    break
                except NoTranscriptFound:
                    continue

            # If no preferred language found, try to get any available transcript
            if transcript is None and self.fallback_to_any:
                try:
                    # Get the first available transcript
                    for t in transcript_list:
                        transcript = t
                        logger.info(f"Using fallback transcript ({t.language_code}) for {video_id}")
                        break
                except Exception:
                    pass

            if transcript is None:
                logger.warning(f"No transcript available for {video_id}")
                return None, "no_captions"

            # Fetch the transcript data
            transcript_data = transcript.fetch()

            # Combine all text segments
            full_text = " ".join([entry.text for entry in transcript_data])

            # Clean up the text
            full_text = self._clean_transcript(full_text)

            logger.info(f"Extracted transcript for {video_id}: {len(full_text)} characters")
            return full_text, "ok"

        except (IpBlocked, RequestBlocked) as e:
            logger.warning(f"YouTube blocked transcript request for {video_id} ({type(e).__name__})")
            return None, "blocked"
        except TranscriptsDisabled:
            logger.warning(f"Transcripts are disabled for video: {video_id}")
            return None, "no_captions"
        except (NoTranscriptFound, VideoUnavailable):
            logger.warning(f"Video unavailable / no transcript: {video_id}")
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

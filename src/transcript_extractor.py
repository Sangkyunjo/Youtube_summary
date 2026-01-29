"""
Transcript extraction module for YouTube videos.
Uses youtube-transcript-api with Korean first, English fallback.
"""

import re
from typing import Optional

from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import (
    TranscriptsDisabled,
    NoTranscriptFound,
    VideoUnavailable,
    CouldNotRetrieveTranscript
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
        self.api = YouTubeTranscriptApi()

    def extract(self, video_id: str) -> Optional[str]:
        """
        Extract transcript from a YouTube video.

        Args:
            video_id: YouTube video ID

        Returns:
            Transcript text or None if unavailable
        """
        logger.info(f"Extracting transcript for video: {video_id}")

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
                return None

            # Fetch the transcript data
            transcript_data = transcript.fetch()

            # Combine all text segments
            full_text = " ".join([entry.text for entry in transcript_data])

            # Clean up the text
            full_text = self._clean_transcript(full_text)

            logger.info(f"Extracted transcript for {video_id}: {len(full_text)} characters")
            return full_text

        except TranscriptsDisabled:
            logger.warning(f"Transcripts are disabled for video: {video_id}")
            return None
        except VideoUnavailable:
            logger.warning(f"Video unavailable: {video_id}")
            return None
        except CouldNotRetrieveTranscript:
            logger.warning(f"Could not retrieve transcript for video: {video_id}")
            return None
        except Exception as e:
            logger.error(f"Error extracting transcript for {video_id}: {e}")
            return None

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

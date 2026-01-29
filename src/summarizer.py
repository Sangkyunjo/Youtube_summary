"""
Summarization module using Anthropic Claude API.
Generates structured summaries with overview, key points, quotes, and takeaways.
"""

from typing import Optional

import anthropic

from utils import load_config, get_env, setup_logging, format_summary

logger = setup_logging("summarizer")


class Summarizer:
    """Generates video summaries using Claude API."""

    SYSTEM_PROMPT = """You are an expert content summarizer. Your task is to analyze video transcripts and create comprehensive, well-structured summaries.

For each transcript, provide:
1. OVERVIEW: A 2-3 paragraph summary of the main topic and content
2. KEY POINTS: 5-8 bullet points covering the most important information
3. NOTABLE QUOTES: 2-4 direct quotes that are particularly insightful or memorable
4. KEY TAKEAWAYS: 3-5 actionable insights or main conclusions

Guidelines:
- Be accurate and faithful to the original content
- Use clear, professional language
- If the content is in Korean, write the summary in Korean
- If the content is in English, write the summary in English
- Maintain the tone and style of the original content
- Focus on substantive information, not filler content"""

    USER_PROMPT_TEMPLATE = """Please analyze the following video transcript and create a structured summary.

Video Title: {title}
Channel: {channel}

TRANSCRIPT:
{transcript}

---

Please provide your summary in the following format:

**OVERVIEW**
[Your overview here]

**KEY POINTS**
- [Point 1]
- [Point 2]
...

**NOTABLE QUOTES**
- "[Quote 1]"
- "[Quote 2]"
...

**KEY TAKEAWAYS**
- [Takeaway 1]
- [Takeaway 2]
..."""

    def __init__(self):
        self.config = load_config()
        anthropic_config = self.config.get("anthropic", {})
        self.model = anthropic_config.get("model", "claude-sonnet-4-20250514")
        self.max_tokens = anthropic_config.get("max_tokens", 4096)

        api_key = get_env("ANTHROPIC_API_KEY")
        if not api_key or api_key == "your_anthropic_api_key_here":
            raise ValueError(
                "ANTHROPIC_API_KEY not configured. "
                "Please set it in config/.env file."
            )

        self.client = anthropic.Anthropic(api_key=api_key)
        logger.info(f"Summarizer initialized with model: {self.model}")

    def summarize(
        self,
        transcript: str,
        title: str,
        channel: str
    ) -> Optional[dict]:
        """
        Generate a summary of a video transcript.

        Args:
            transcript: The video transcript text
            title: Video title
            channel: Channel name

        Returns:
            Dictionary with summary sections or None if failed
        """
        logger.info(f"Generating summary for: {title}")

        # Truncate transcript if too long (Claude has context limits)
        max_transcript_length = 100000  # ~100k characters
        if len(transcript) > max_transcript_length:
            logger.warning(f"Transcript truncated from {len(transcript)} to {max_transcript_length} chars")
            transcript = transcript[:max_transcript_length] + "... [truncated]"

        try:
            user_prompt = self.USER_PROMPT_TEMPLATE.format(
                title=title,
                channel=channel,
                transcript=transcript
            )

            message = self.client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=self.SYSTEM_PROMPT,
                messages=[
                    {"role": "user", "content": user_prompt}
                ]
            )

            response_text = message.content[0].text
            logger.info(f"Summary generated for: {title}")

            # Parse the response into sections
            sections = self._parse_response(response_text)

            return sections

        except anthropic.APIConnectionError as e:
            logger.error(f"API connection error: {e}")
            return None
        except anthropic.RateLimitError as e:
            logger.error(f"Rate limit exceeded: {e}")
            return None
        except anthropic.APIStatusError as e:
            logger.error(f"API status error: {e}")
            return None
        except Exception as e:
            logger.error(f"Error generating summary: {e}")
            return None

    def _parse_response(self, response: str) -> dict:
        """
        Parse the Claude response into sections.

        Args:
            response: Raw response text from Claude

        Returns:
            Dictionary with parsed sections
        """
        sections = {
            "overview": "",
            "key_points": "",
            "quotes": "",
            "takeaways": "",
            "raw_response": response
        }

        # Try to extract sections using markers
        import re

        # Extract overview
        overview_match = re.search(
            r'\*\*OVERVIEW\*\*\s*(.*?)(?=\*\*KEY POINTS\*\*|\*\*NOTABLE|\*\*KEY TAKEAWAYS|$)',
            response,
            re.DOTALL | re.IGNORECASE
        )
        if overview_match:
            sections["overview"] = overview_match.group(1).strip()

        # Extract key points
        key_points_match = re.search(
            r'\*\*KEY POINTS\*\*\s*(.*?)(?=\*\*NOTABLE|\*\*KEY TAKEAWAYS|$)',
            response,
            re.DOTALL | re.IGNORECASE
        )
        if key_points_match:
            sections["key_points"] = key_points_match.group(1).strip()

        # Extract quotes
        quotes_match = re.search(
            r'\*\*NOTABLE QUOTES\*\*\s*(.*?)(?=\*\*KEY TAKEAWAYS|$)',
            response,
            re.DOTALL | re.IGNORECASE
        )
        if quotes_match:
            sections["quotes"] = quotes_match.group(1).strip()

        # Extract takeaways
        takeaways_match = re.search(
            r'\*\*KEY TAKEAWAYS\*\*\s*(.*?)$',
            response,
            re.DOTALL | re.IGNORECASE
        )
        if takeaways_match:
            sections["takeaways"] = takeaways_match.group(1).strip()

        # If parsing failed, put everything in overview
        if not sections["overview"]:
            sections["overview"] = response

        return sections

    def create_summary_text(
        self,
        title: str,
        channel: str,
        published: str,
        url: str,
        sections: dict
    ) -> str:
        """
        Create the final formatted summary text.

        Args:
            title: Video title
            channel: Channel name
            published: Published date string
            url: Video URL
            sections: Dictionary with summary sections

        Returns:
            Formatted summary text
        """
        return format_summary(
            title=title,
            channel=channel,
            published=published,
            url=url,
            overview=sections.get("overview", ""),
            key_points=sections.get("key_points", ""),
            quotes=sections.get("quotes", ""),
            takeaways=sections.get("takeaways", "")
        )


def main():
    """Test the summarizer."""
    try:
        summarizer = Summarizer()

        # Test with a sample transcript
        test_transcript = """
        Hello everyone, welcome to today's video. Today we're going to talk about
        artificial intelligence and how it's changing the world. AI is transforming
        every industry from healthcare to finance. Machine learning allows computers
        to learn from data without being explicitly programmed. Deep learning uses
        neural networks to process complex patterns. The future of AI is both exciting
        and challenging. We need to consider ethical implications. Thank you for watching!
        """

        sections = summarizer.summarize(
            transcript=test_transcript,
            title="AI and the Future",
            channel="Test Channel"
        )

        if sections:
            print("Summary generated successfully!")
            print(f"\nOverview:\n{sections['overview']}")
            print(f"\nKey Points:\n{sections['key_points']}")
        else:
            print("Failed to generate summary")

    except ValueError as e:
        print(f"Configuration error: {e}")


if __name__ == "__main__":
    main()

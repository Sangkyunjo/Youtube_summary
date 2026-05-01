"""
Summarization module using an OpenAI-compatible LLM API.
Supports MiniMax (default) and OpenAI providers via config.
Generates structured summaries with overview, key points, quotes, and takeaways.
"""

from typing import Optional

import openai

from utils import load_config, get_env, setup_logging, format_summary

logger = setup_logging("summarizer")


class Summarizer:
    """Generates video summaries via an OpenAI-compatible LLM API."""

    SYSTEM_PROMPT = """You are an expert content summarizer. Your task is to analyze video transcripts and create comprehensive, well-structured summaries.

For each transcript, provide:
1. 개요: A 2-3 paragraph summary of the main topic and content
2. 핵심 포인트: 5-8 bullet points covering the most important information
3. 주요 인용구: 2-4 direct quotes that are particularly insightful or memorable
4. 핵심 요점: 3-5 actionable insights or main conclusions

Guidelines:
- Always write the summary in Korean, regardless of the transcript language
- Be accurate and faithful to the original content
- Use clear, professional Korean language
- Maintain the tone and style of the original content
- Focus on substantive information, not filler content"""

    USER_PROMPT_TEMPLATE = """다음 영상 스크립트를 분석하여 구조화된 요약을 한국어로 작성해 주세요.

영상 제목: {title}
채널: {channel}

스크립트:
{transcript}

---

다음 형식으로 요약을 제공해 주세요:

**개요**
[개요 내용]

**핵심 포인트**
- [포인트 1]
- [포인트 2]
...

**주요 인용구**
- "[인용구 1]"
- "[인용구 2]"
...

**핵심 요점**
- [요점 1]
- [요점 2]
..."""

    PROVIDER_DEFAULTS = {
        "minimax": {
            "model": "MiniMax-M2.7",
            "max_tokens": 4096,
            "base_url": "https://api.minimax.io/v1",
            "api_key_env": "MINIMAX_API_KEY",
        },
        "openai": {
            "model": "gpt-4.1-mini",
            "max_tokens": 4096,
            "base_url": None,
            "api_key_env": "OPENAI_API_KEY",
        },
    }

    def __init__(self):
        self.config = load_config()
        provider = self.config.get("llm", {}).get("provider", "minimax").lower()
        if provider not in self.PROVIDER_DEFAULTS:
            raise ValueError(
                f"Unsupported llm.provider '{provider}'. "
                f"Supported: {list(self.PROVIDER_DEFAULTS)}"
            )
        self.provider = provider

        defaults = self.PROVIDER_DEFAULTS[provider]
        # `or {}` so an empty `minimax:` block in YAML (parsed as None) doesn't crash.
        provider_config = self.config.get(provider) or {}
        self.model = provider_config.get("model", defaults["model"])
        self.max_tokens = provider_config.get("max_tokens", defaults["max_tokens"])
        base_url = provider_config.get("base_url", defaults["base_url"])
        api_key_env = defaults["api_key_env"]

        # Reject http:// to avoid leaking the API key over plaintext if config is tampered with.
        if base_url and not base_url.startswith("https://"):
            raise ValueError(
                f"{provider}.base_url must use https:// (got: {base_url!r})"
            )

        api_key = (get_env(api_key_env) or "").strip()
        if not api_key or api_key.startswith("your_"):
            raise ValueError(
                f"{api_key_env} not configured. "
                "Please set it in config/.env file."
            )

        client_kwargs = {"api_key": api_key}
        if base_url:
            client_kwargs["base_url"] = base_url
        self.client = openai.OpenAI(**client_kwargs)
        logger.info(
            f"Summarizer initialized - provider: {self.provider}, "
            f"model: {self.model}"
        )

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

        # Truncate transcript if too long (LLM context limits)
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

            response = self.client.chat.completions.create(
                model=self.model,
                max_tokens=self.max_tokens,
                messages=[
                    {"role": "system", "content": self.SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt}
                ]
            )

            response_text = response.choices[0].message.content
            logger.info(f"Summary generated for: {title}")

            # Parse the response into sections
            sections = self._parse_response(response_text)

            return sections

        except openai.APIConnectionError as e:
            logger.error(f"API connection error: {e}")
            return None
        except openai.RateLimitError as e:
            logger.error(f"Rate limit exceeded: {e}")
            return None
        except openai.APIStatusError as e:
            logger.error(f"API status error: {e}")
            return None
        except Exception as e:
            logger.error(f"Error generating summary: {e}")
            return None

    def _parse_response(self, response: str) -> dict:
        """
        Parse the LLM response into sections.

        Args:
            response: Raw response text from the LLM

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
            r'\*\*개요\*\*\s*(.*?)(?=\*\*핵심 포인트\*\*|\*\*주요 인용구\*\*|\*\*핵심 요점\*\*|$)',
            response,
            re.DOTALL
        )
        if overview_match:
            sections["overview"] = overview_match.group(1).strip()

        # Extract key points
        key_points_match = re.search(
            r'\*\*핵심 포인트\*\*\s*(.*?)(?=\*\*주요 인용구\*\*|\*\*핵심 요점\*\*|$)',
            response,
            re.DOTALL
        )
        if key_points_match:
            sections["key_points"] = key_points_match.group(1).strip()

        # Extract quotes
        quotes_match = re.search(
            r'\*\*주요 인용구\*\*\s*(.*?)(?=\*\*핵심 요점\*\*|$)',
            response,
            re.DOTALL
        )
        if quotes_match:
            sections["quotes"] = quotes_match.group(1).strip()

        # Extract takeaways
        takeaways_match = re.search(
            r'\*\*핵심 요점\*\*\s*(.*?)$',
            response,
            re.DOTALL
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

import unittest
from unittest.mock import patch

import summarizer


class SummarizerProviderTest(unittest.TestCase):
    def test_qwen_is_the_default_provider(self):
        client = object()
        with (
            patch.object(summarizer, "load_config", return_value={}),
            patch.object(summarizer, "get_env", return_value="qwen-test-key"),
            patch.object(summarizer.openai, "OpenAI", return_value=client) as openai_client,
        ):
            instance = summarizer.Summarizer()

        self.assertEqual(instance.provider, "qwen")
        self.assertEqual(instance.model, "qwen-plus")
        self.assertEqual(instance.max_tokens, 4096)
        self.assertIs(instance.client, client)
        openai_client.assert_called_once_with(
            api_key="qwen-test-key",
            base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        )

    def test_repository_config_selects_qwen(self):
        config = summarizer.load_config()

        self.assertEqual(config["llm"]["provider"], "qwen")
        self.assertEqual(config["qwen"]["model"], "qwen-plus")


if __name__ == "__main__":
    unittest.main()

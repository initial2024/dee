from __future__ import annotations

import json
import unittest

from codex_ai_router.response_compat import ResponseCompatibilityError, diagnostic_headers, extract_visible_text, iter_normalized_sse, normalize_chat_completion


class ResponseCompatibilityTests(unittest.TestCase):
    def test_message_content_is_normalized(self):
        result = normalize_chat_completion({"model": "m", "choices": [{"message": {"content": "visible"}}]}, created=1)
        self.assertEqual((result["object"], result["choices"][0]["message"]["content"]), ("chat.completion", "visible"))

    def test_supported_non_stream_shapes_extract_visible_text(self):
        shapes = [
            {"choices": [{"delta": {"content": "a"}}]},
            {"output_text": "b"},
            {"content": [{"text": "c"}]},
            {"message": {"content": [{"text": "d"}]}},
            {"response": {"output": [{"content": [{"text": "e"}]}]}},
        ]
        self.assertEqual([extract_visible_text(value) for value in shapes], ["a", "b", "c", "d", "e"])

    def test_additional_text_aliases_extract_visible_text(self):
        shapes = [{"text": "a"}, {"completion": "b"}, {"result": "c"}, {"answer": "d"}]
        self.assertEqual([extract_visible_text(value) for value in shapes], ["a", "b", "c", "d"])

    def test_empty_success_is_explicit_error(self):
        with self.assertRaisesRegex(ResponseCompatibilityError, "UPSTREAM_CONTENT_EMPTY"):
            normalize_chat_completion({"choices": [{"message": {"content": []}}]})

    def test_sse_is_incremental_and_terminates(self):
        source = iter(["data: {\"choices\":[{\"delta\":{\"content\":\"A\"}}]}\n", "data: {\"choices\":[{\"delta\":{\"content\":\"B\"}}]}\n", "data: [DONE]\n"])
        mapped = iter_normalized_sse(source, model="deepseek-web", created=1)
        first = next(mapped)
        self.assertIn('"content":"A"', first)
        rest = "".join(mapped)
        self.assertIn('"content":"B"', rest)
        self.assertIn('"finish_reason":"stop"', rest)
        self.assertTrue(rest.endswith("data: [DONE]\n\n"))

    def test_empty_sse_is_not_success(self):
        output = "".join(iter_normalized_sse(["data: {}\n", "data: [DONE]\n"], created=1))
        self.assertIn("UPSTREAM_CONTENT_EMPTY", output)

    def test_diagnostic_headers_are_content_free(self):
        headers = diagnostic_headers(upstream_status=200, upstream_content_type="application/json", endpoint_mode="CUSTOM_ROUTER", normalized=True, content_detected=True, stream_mode="stream")
        self.assertEqual(headers["X-Xiaoyu-Content-Detected"], "yes")
        self.assertNotIn("prompt", json.dumps(headers).lower())


if __name__ == "__main__":
    unittest.main()

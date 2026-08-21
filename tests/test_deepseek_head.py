from __future__ import annotations

import json
import unittest

from codex_ai_router.deepseek_head import run_deepseek_head


class FakeResponse:
    def __init__(self, body: dict, status: int = 200): self._body, self.status = body, status
    def read(self): return json.dumps(self._body).encode("utf-8")
    def __enter__(self): return self
    def __exit__(self, *_args): return False


class DeepSeekHeadTests(unittest.TestCase):
    def test_offline_bridge_is_reported_before_post(self):
        def offline(_request, timeout=0): raise OSError("offline")
        result = run_deepseek_head("plan", opener=offline)
        self.assertEqual((result["status"], result["error_code"]), ("BRIDGE_OFFLINE", "BRIDGE_OFFLINE"))

    def test_busy_bridge_is_reported_before_post(self):
        result = run_deepseek_head("plan", opener=lambda *_args, **_kwargs: FakeResponse({"busy": True}))
        self.assertEqual(result["status"], "BRIDGE_BUSY")

    def test_visible_result_is_advisory_only(self):
        replies = iter([FakeResponse({"busy": False}), FakeResponse({"choices": [{"message": {"content": "analysis"}}]})])
        result = run_deepseek_head("plan", task_type="架构设计", api_key="fixture", opener=lambda *_args, **_kwargs: next(replies))
        self.assertEqual((result["status"], result["model"], result["stream_mode"]), ("PASS", "deepseek-web", "non_stream"))
        self.assertIn("Review the following", result["codex_instruction"])
        self.assertTrue(result["needs_human_confirmation"])
        self.assertIn("No command", result["powershell_command"])

    def test_empty_upstream_is_not_success(self):
        replies = iter([FakeResponse({"busy": False}), FakeResponse({"choices": [{"message": {"content": []}}]})])
        result = run_deepseek_head("plan", opener=lambda *_args, **_kwargs: next(replies))
        self.assertEqual(result["status"], "UPSTREAM_CONTENT_EMPTY")

    def test_non_loopback_target_is_rejected(self):
        self.assertEqual(run_deepseek_head("plan", api_base="https://example.invalid/v1")["status"], "INVALID_LOCAL_REQUEST")


if __name__ == "__main__":
    unittest.main()

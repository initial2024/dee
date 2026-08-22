from __future__ import annotations

import json
import unittest
from urllib.request import Request

from codex_ai_router.deepseek_bridge_direct import run_deepseek_bridge_direct


class FakeResponse:
    def __init__(self, body: dict, status: int = 200):
        self.body = body
        self.status = status

    def read(self):
        return json.dumps(self.body, ensure_ascii=False).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class DirectBridgeTests(unittest.TestCase):
    @staticmethod
    def ready_health(**overrides):
        health = {
            "ok": True,
            "deepseek": {"pageReady": True, "loggedIn": True, "loginRequired": False, "inputReady": True},
            "bridge": {"busy": False},
        }
        health.update(overrides)
        return health

    def test_non_loopback_endpoints_are_rejected(self):
        for url in ("https://127.0.0.1:8791/v1", "http://localhost:8791/v1", "http://192.168.137.1:8791/v1", "http://127.0.0.1:8792/v1"):
            with self.subTest(url=url):
                result = run_deepseek_bridge_direct("plan", api_base=url)
                self.assertEqual(result["error_code"], "DEEPSEEK_MODE_UNAVAILABLE")

    def test_offline_bridge_is_reported_without_post(self):
        calls = []

        def offline(request, **_kwargs):
            calls.append(request)
            raise OSError("offline")

        result = run_deepseek_bridge_direct("plan", opener=offline)
        self.assertEqual(result["error_code"], "DEEPSEEK_BRIDGE_OFFLINE")
        self.assertEqual(len(calls), 1)

    def test_health_gates_login_busy_and_mode(self):
        for health, expected in (
            ({"ok": True, "bridge": {"busy": True}, "deepseek": {}}, "DEEPSEEK_BRIDGE_BUSY"),
            ({"ok": False, "bridge": {"busy": False}, "deepseek": {"loggedIn": False, "loginRequired": True}}, "DEEPSEEK_LOGIN_REQUIRED"),
            ({"ok": True, "bridge": {"busy": False}, "deepseek": {"loggedIn": True, "pageReady": False, "inputReady": False}}, "DEEPSEEK_MODE_UNAVAILABLE"),
        ):
            with self.subTest(expected=expected):
                result = run_deepseek_bridge_direct("plan", opener=lambda *_args, **_kwargs: FakeResponse(health))
                self.assertEqual(result["error_code"], expected)

    def test_direct_post_is_text_only_and_loopback(self):
        requests: list[Request | str] = []
        responses = iter((FakeResponse(self.ready_health()), FakeResponse({"choices": [{"message": {"content": "PLAN_OK"}}]})))

        def opener(request, **_kwargs):
            requests.append(request)
            return next(responses)

        result = run_deepseek_bridge_direct("只生成计划", opener=opener)
        self.assertEqual((result["status"], result["analysis"]), ("PASS", "PLAN_OK"))
        self.assertEqual(requests[1].full_url, "http://127.0.0.1:8791/v1/chat/completions")
        payload = json.loads(requests[1].data.decode("utf-8"))
        self.assertNotIn("tools", payload)
        self.assertNotIn("tool_choice", payload)
        self.assertNotIn("function_call", payload)
        self.assertNotIn("functions", payload)
        self.assertEqual(payload["stream"], False)
        self.assertIn("TEXT_ONLY", payload["messages"][0]["content"])
        self.assertEqual(result["prompt_response_logged"], "NO")
        self.assertEqual(result["secrets_logged"], "NO")

    def test_empty_response_is_not_success(self):
        responses = iter((FakeResponse(self.ready_health()), FakeResponse({"choices": [{"message": {"content": ""}}]})))
        result = run_deepseek_bridge_direct("plan", opener=lambda *_args, **_kwargs: next(responses))
        self.assertEqual(result["error_code"], "DEEPSEEK_EMPTY_RESPONSE")


if __name__ == "__main__":
    unittest.main()

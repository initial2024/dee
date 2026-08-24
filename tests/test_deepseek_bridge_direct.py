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

    @staticmethod
    def ready_mode_probe():
        return {"quick_available": True, "expert_available": True, "thinking_available": True, "search_available": True, "vision_available": False, "file_upload_available": False, "current_base_mode": "quick", "current_thinking": False, "current_search": False, "current_modality": "text", "ui_changed": False, "modes": {name: {"status": "AVAILABLE" if name in {"quick", "expert", "thinking", "search"} else "UNAVAILABLE", "controllable": name in {"quick", "expert", "thinking", "search"}} for name in ("quick", "expert", "thinking", "search", "vision", "file")}}

    def test_non_loopback_endpoints_are_rejected(self):
        for url in ("https://127.0.0.1:8791/v1", "http://192.168.137.1:8791/v1", "http://127.0.0.1:8792/v1"):
            with self.subTest(url=url):
                result = run_deepseek_bridge_direct("plan", api_base=url)
                self.assertEqual(result["error_code"], "DEEPSEEK_BRIDGE_DIRECT_URL_NOT_CONFIGURED")

    def test_offline_bridge_is_reported_without_post(self):
        calls = []

        def offline(request, **_kwargs):
            calls.append(request)
            raise OSError("offline")

        result = run_deepseek_bridge_direct("plan", opener=offline)
        self.assertEqual(result["error_code"], "DEEPSEEK_BRIDGE_DIRECT_UNAVAILABLE")
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
        responses = iter((FakeResponse(self.ready_health()), FakeResponse(self.ready_mode_probe()), FakeResponse({"choices": [{"message": {"content": "PLAN_OK"}}]})))

        def opener(request, **_kwargs):
            requests.append(request)
            return next(responses)

        result = run_deepseek_bridge_direct("只生成计划", opener=opener)
        self.assertEqual((result["status"], result["analysis"]), ("PASS", "PLAN_OK"))
        self.assertEqual(requests[1], "http://127.0.0.1:8791/mode-probe")
        self.assertEqual(requests[2].full_url, "http://127.0.0.1:8791/v1/chat/completions")
        payload = json.loads(requests[2].data.decode("utf-8"))
        self.assertNotIn("tools", payload)
        self.assertNotIn("tool_choice", payload)
        self.assertNotIn("function_call", payload)
        self.assertNotIn("functions", payload)
        self.assertEqual(payload["stream"], False)
        self.assertIn("TEXT_ONLY", payload["messages"][0]["content"])
        self.assertEqual(result["prompt_response_logged"], "NO")
        self.assertEqual(result["secrets_logged"], "NO")

    def test_empty_response_is_not_success(self):
        responses = iter((FakeResponse(self.ready_health()), FakeResponse(self.ready_mode_probe()), FakeResponse({"choices": [{"message": {"content": ""}}]})))
        result = run_deepseek_bridge_direct("plan", opener=lambda *_args, **_kwargs: next(responses))
        self.assertEqual(result["error_code"], "DEEPSEEK_EMPTY_RESPONSE")

    def test_invalid_explicit_mode_is_pre_send_error_without_chat_post(self):
        calls = []

        def opener(request, **_kwargs):
            calls.append(request)
            raise AssertionError("a pre-send validation must not call the bridge")

        result = run_deepseek_bridge_direct("plan", selected_mode="not-a-mode", opener=opener)
        self.assertEqual(result["error_code"], "DEEPSEEK_BRIDGE_DIRECT_MODE_PARAM_INVALID")
        self.assertEqual((result["provider_error_stage"], result["bridge_send_attempted"], result["model_output_available"]), ("before_bridge_send", "NO", "NO"))
        self.assertEqual(calls, [])

    def test_explicit_expert_thinking_requires_matching_probe_before_post(self):
        requests = []
        probe = self.ready_mode_probe()
        probe.update({"current_base_mode": "expert", "current_thinking": True, "current_search": False})
        responses = iter((FakeResponse(self.ready_health()), FakeResponse(probe), FakeResponse({"choices": [{"message": {"content": "PLAN_OK"}}]})))

        def opener(request, **_kwargs):
            requests.append(request)
            return next(responses)

        result = run_deepseek_bridge_direct("只生成计划", selected_mode="expert_thinking", search=False, opener=opener)
        self.assertEqual((result["status"], result["selected_mode"], result["bridge_send_attempted"]), ("PASS", "expert_thinking", "YES"))
        payload = json.loads(requests[2].data.decode("utf-8"))
        self.assertEqual(payload["model"], "deepseek-web-expert-thinking")
        self.assertNotIn("tools", payload)
        self.assertNotIn("tool_choice", payload)

    def test_mode_mismatch_is_pre_send_error_without_chat_post(self):
        requests = []
        responses = iter((FakeResponse(self.ready_health()), FakeResponse(self.ready_mode_probe())))

        def opener(request, **_kwargs):
            requests.append(request)
            return next(responses)

        result = run_deepseek_bridge_direct("plan", selected_mode="expert_thinking", search=False, opener=opener)
        self.assertEqual(result["error_code"], "DEEPSEEK_BRIDGE_DIRECT_MODE_PARAM_INVALID")
        self.assertEqual((result["provider_error_stage"], result["bridge_send_attempted"], result["bridge_ui_send_attempt_count"]), ("before_bridge_send", "NO", 0))
        self.assertEqual(len(requests), 2)


if __name__ == "__main__":
    unittest.main()

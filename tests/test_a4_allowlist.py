from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_ai_router.provider_allowlist import model_catalog
from codex_ai_router.server import RouterService


class FakeUpstream:
    status = 200

    def __init__(self, payload: dict):
        self.payload = payload

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class FakeStreamingUpstream(FakeUpstream):
    def __init__(self):
        super().__init__({})

    def __iter__(self):
        return iter([b'data: {"choices":[{"delta":{"content":"visible"}}]}\n', b'data: [DONE]\n'])


class A4AllowlistTests(unittest.TestCase):
    def test_disabled_external_provider_is_not_catalogued(self):
        records = [{"id": "external", "type": "EXTERNAL_API_ALLOWED", "endpoint": "https://example.invalid/v1", "models": ["external-fast"], "enabled": False, "status": "DISABLED"}]
        with patch("codex_ai_router.provider_allowlist.providers", return_value=records):
            self.assertNotIn("external-fast", {item["id"] for item in model_catalog()})

    def test_manual_plan_is_structured_without_backend_call(self):
        provider = {"id": "manual-plan", "type": "MANUAL_PLAN", "endpoint": None, "models": ["manual-plan"], "enabled": True, "status": "ENABLED"}
        service = RouterService()
        with patch("codex_ai_router.server.allowlisted_providers", return_value=[provider]), patch("codex_ai_router.server.resolve_allowlisted", return_value=(provider, None)):
            result = service.respond({"model": "manual-plan", "input": "inspect the diff"})
        text = result["output_text"]
        self.assertIn("Problem Summary:", text)
        self.assertIn("Requires User Confirmation: YES", text)

    def test_tools_are_rejected_instead_of_faked(self):
        service = RouterService(tools_policy="strict_reject")
        with self.assertRaisesRegex(RuntimeError, "TOOLS_NOT_SUPPORTED_BY_BACKEND"):
            service.respond({"model": "deepseek-web", "input": "hello", "tools": [{"type": "function"}]})

    def test_allowlisted_chat_is_normalized_and_ledger_has_metadata_only(self):
        provider = {"id": "bridge", "type": "DEEPSEEK_WEB_BRIDGE", "endpoint": "http://127.0.0.1:8792/v1", "models": ["deepseek-web"], "enabled": True, "status": "ENABLED"}
        with tempfile.TemporaryDirectory() as temp:
            ledger = Path(temp) / "calls.jsonl"
            with patch.dict(os.environ, {"XIAOYU_ROUTER_CALL_LEDGER": str(ledger), "XIAOYU_ROUTER_BRIDGE_API_KEY": "fixture-key"}, clear=False), patch("codex_ai_router.server.allowlisted_providers", return_value=[provider]), patch("codex_ai_router.server.resolve_allowlisted", return_value=(provider, None)), patch("codex_ai_router.server.urlopen", return_value=FakeUpstream({"choices": [{"message": {"content": "visible"}}]})):
                result = RouterService().chat_completion({"model": "deepseek-web", "messages": [{"role": "user", "content": "hello"}]})
            self.assertEqual(result["choices"][0]["message"]["content"], "visible")
            raw = ledger.read_text(encoding="utf-8")
            self.assertIn('"saved_body":false', raw)
            self.assertNotIn("hello", raw)
            self.assertNotIn("visible", raw)

    def test_missing_bridge_key_is_auth_missing_without_upstream_call(self):
        provider = {"id": "bridge", "type": "DEEPSEEK_WEB_BRIDGE", "endpoint": "http://127.0.0.1:8792/v1", "models": ["deepseek-web"], "enabled": True, "status": "ENABLED"}
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("XIAOYU_ROUTER_BRIDGE_API_KEY", None)
            with patch("codex_ai_router.server.allowlisted_providers", return_value=[provider]), patch("codex_ai_router.server.resolve_allowlisted", return_value=(provider, None)), patch("codex_ai_router.server.urlopen") as open_:
                with self.assertRaisesRegex(RuntimeError, "AUTH_MISSING"):
                    RouterService().chat_completion({"model": "deepseek-web", "messages": [{"role": "user", "content": "hello"}]})
            open_.assert_not_called()

    def test_bridge_key_is_forwarded_only_as_authorization_header(self):
        provider = {"id": "bridge", "type": "DEEPSEEK_WEB_BRIDGE", "endpoint": "http://127.0.0.1:8792/v1", "models": ["deepseek-web"], "enabled": True, "status": "ENABLED"}
        captured = {}

        def opener(request, timeout=0):
            captured["authorization"] = request.headers.get("Authorization")
            return FakeUpstream({"choices": [{"message": {"content": "visible"}}]})

        with patch.dict(os.environ, {"XIAOYU_ROUTER_BRIDGE_API_KEY": "fixture-key"}, clear=False), patch("codex_ai_router.server.allowlisted_providers", return_value=[provider]), patch("codex_ai_router.server.resolve_allowlisted", return_value=(provider, None)), patch("codex_ai_router.server.urlopen", side_effect=opener):
            RouterService().chat_completion({"model": "deepseek-web", "messages": [{"role": "user", "content": "hello"}]})
        self.assertEqual(captured["authorization"], "Bearer fixture-key")

    def test_deepseek_bridge_compacts_codex_context_to_last_user_sentence(self):
        provider = {"id": "bridge", "type": "DEEPSEEK_WEB_BRIDGE", "endpoint": "http://127.0.0.1:8792/v1", "models": ["deepseek-web"], "enabled": True, "status": "ENABLED"}
        with patch.dict(os.environ, {"XIAOYU_ROUTER_BRIDGE_API_KEY": "fixture-key"}, clear=False), patch("codex_ai_router.server.allowlisted_providers", return_value=[provider]), patch("codex_ai_router.server.resolve_allowlisted", return_value=(provider, None)), patch("codex_ai_router.server.urlopen", return_value=FakeUpstream({"choices": [{"message": {"content": "visible"}}]})) as open_:
            RouterService(tools_policy="text_only_strip").respond({
                "model": "deepseek-web",
                "input": [{"role": "user", "content": "<INSTRUCTIONS>internal context</INSTRUCTIONS>\n<environment_context>machine details</environment_context>\n只回复 ONE"}],
                "instructions": "another internal instruction",
            })
        outbound = json.loads(open_.call_args.args[0].data.decode("utf-8"))
        self.assertEqual(outbound["messages"], [{"role": "user", "content": "只回复 ONE"}])
        self.assertNotIn("INSTRUCTIONS", json.dumps(outbound, ensure_ascii=False))
        self.assertNotIn("environment_context", json.dumps(outbound, ensure_ascii=False))

    def test_deepseek_bridge_stream_also_compacts_codex_context(self):
        provider = {"id": "bridge", "type": "DEEPSEEK_WEB_BRIDGE", "endpoint": "http://127.0.0.1:8792/v1", "models": ["deepseek-web"], "enabled": True, "status": "ENABLED"}
        with patch.dict(os.environ, {"XIAOYU_ROUTER_BRIDGE_API_KEY": "fixture-key"}, clear=False), patch("codex_ai_router.server.allowlisted_providers", return_value=[provider]), patch("codex_ai_router.server.resolve_allowlisted", return_value=(provider, None)), patch("codex_ai_router.server.urlopen", return_value=FakeStreamingUpstream()) as open_:
            list(RouterService().stream_chat({
                "model": "deepseek-web",
                "messages": [{"role": "user", "content": "<environment_context>machine details</environment_context>\n只回复 STREAM_ONE"}],
                "stream": True,
            }))
        outbound = json.loads(open_.call_args.args[0].data.decode("utf-8"))
        self.assertEqual(outbound["messages"], [{"role": "user", "content": "只回复 STREAM_ONE"}])

    def test_deepseek_auto_strips_tools_before_mode_selection_and_forwards_alias(self):
        provider = {
            "id": "bridge",
            "type": "DEEPSEEK_WEB_BRIDGE",
            "endpoint": "http://127.0.0.1:8792/v1",
            "models": ["deepseek-web-auto"],
            "mode_availability": {
                "normal": {"status": "AVAILABLE", "controllable": True},
                "search": {"status": "AVAILABLE", "controllable": True},
                "thinking": {"status": "UI_PROBE_FAILED", "controllable": False},
                "expert": {"status": "AVAILABLE", "controllable": True},
            },
            "enabled": True,
            "status": "ENABLED",
        }
        with patch.dict(os.environ, {"XIAOYU_ROUTER_BRIDGE_API_KEY": "fixture-key"}, clear=False), patch("codex_ai_router.server.allowlisted_providers", return_value=[provider]), patch("codex_ai_router.server.resolve_allowlisted", return_value=(provider, None)), patch("codex_ai_router.server.urlopen", return_value=FakeUpstream({"choices": [{"message": {"content": "visible"}}]})) as open_:
            result = RouterService(tools_policy="text_only_strip").respond({
                "model": "deepseek-web-auto",
                "input": "请搜索最新 API 文档",
                "tools": [{"type": "function", "function": {"name": "not_forwarded"}}],
                "tool_choice": "auto",
            })
        self.assertEqual(result["output_text"], "visible")
        outbound = json.loads(open_.call_args.args[0].data.decode("utf-8"))
        self.assertEqual(outbound["model"], "deepseek-web-search")
        self.assertNotIn("tools", outbound)

    def test_missing_external_key_is_auth_missing(self):
        provider = {"id": "external", "type": "EXTERNAL_API_ALLOWED", "endpoint": "https://example.invalid/v1", "models": ["external-fast"], "enabled": True, "status": "AUTH_MISSING"}
        with patch("codex_ai_router.server.allowlisted_providers", return_value=[provider]), patch("codex_ai_router.server.resolve_allowlisted", return_value=(provider, "AUTH_MISSING")):
            with self.assertRaisesRegex(RuntimeError, "AUTH_MISSING"):
                RouterService().respond({"model": "external-fast", "input": "hello"})


if __name__ == "__main__":
    unittest.main()

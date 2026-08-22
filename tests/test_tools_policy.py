from __future__ import annotations

import os
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_ai_router.server import RouterService
from codex_ai_router.tools_policy import MANUAL_PLAN, STRICT_REJECT, TEXT_ONLY_STRIP, load_policy, save_policy


class RecordingLocal:
    def __init__(self):
        self.prompts: list[str] = []

    def available(self):
        return True

    def ask(self, prompt):
        self.prompts.append(prompt)
        return "TEXT_ONLY_VISIBLE"


class FakeUpstream:
    status = 200

    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class ToolsPolicyTests(unittest.TestCase):
    def test_default_policy_is_strict_reject(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.dict(os.environ, {"XIAOYU_ROUTER_TOOLS_POLICY_FILE": str(Path(temp) / "missing.json")}, clear=False):
                service = RouterService(local=RecordingLocal())
            self.assertEqual(service.tools_policy, STRICT_REJECT)
            with self.assertRaisesRegex(RuntimeError, "TOOLS_NOT_SUPPORTED_BY_BACKEND"):
                service.respond({"model": "xiaoyu-local", "input": "hello", "tools": [{"type": "function"}]})

    def test_text_only_strip_removes_tools_and_injects_guard(self):
        local = RecordingLocal()
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"XIAOYU_ROUTER_TOOLS_POLICY_FILE": str(Path(temp) / "policy.json")}, clear=False):
            save_policy(TEXT_ONLY_STRIP)
            result = RouterService(local=local).respond({
                "model": "xiaoyu-local",
                "input": "只回复 TEXT_ONLY_OK",
                "tools": [{"type": "function", "function": {"name": "danger"}}],
                "tool_choice": "auto",
                "function_call": {"name": "danger"},
                "functions": [{"name": "danger"}],
            })
        self.assertEqual(result["output_text"], "TEXT_ONLY_VISIBLE")
        self.assertEqual(len(local.prompts), 1)
        self.assertIn("不能声称已经读取、修改、运行、提交或部署", local.prompts[0])
        self.assertNotIn("danger", local.prompts[0])
        self.assertNotIn("tool_calls", result)

    def test_manual_plan_does_not_call_model(self):
        local = RecordingLocal()
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"XIAOYU_ROUTER_TOOLS_POLICY_FILE": str(Path(temp) / "policy.json")}, clear=False):
            save_policy(MANUAL_PLAN)
            result = RouterService(local=local).respond({
                "model": "xiaoyu-local",
                "input": "检查这个任务",
                "tools": [{"type": "function"}],
            })
        self.assertEqual(local.prompts, [])
        self.assertIn("Requires Official Codex Tools: YES", result["output_text"])
        self.assertNotIn("tool_calls", result)

    def test_manual_plan_policy_never_calls_model_without_tools_either(self):
        local = RecordingLocal()
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"XIAOYU_ROUTER_TOOLS_POLICY_FILE": str(Path(temp) / "policy.json")}, clear=False):
            save_policy(MANUAL_PLAN)
            result = RouterService(local=local).respond({"model": "xiaoyu-local", "input": "普通文本任务"})
        self.assertEqual(local.prompts, [])
        self.assertIn("Problem Summary: 普通文本任务", result["output_text"])

    def test_deepseek_and_hybrid_text_only_are_visible_without_tool_forwarding(self):
        provider = {"id": "bridge", "type": "DEEPSEEK_WEB_BRIDGE", "endpoint": "http://127.0.0.1:8792/v1", "models": ["deepseek-web"], "enabled": True, "status": "ENABLED"}
        for model in ("deepseek-web", "hybrid-agent"):
            with patch("codex_ai_router.server.allowlisted_providers", return_value=[provider]), patch("codex_ai_router.server.resolve_allowlisted", return_value=(provider, None)), patch("codex_ai_router.server.urlopen", return_value=FakeUpstream({"choices": [{"message": {"content": "DEEPSEEK_TEXT_ONLY_VISIBLE"}}]})) as open_:
                result = RouterService(tools_policy=TEXT_ONLY_STRIP).respond({"model": model, "input": "只回复 TEXT_ONLY_OK", "tools": [{"type": "function"}], "tool_choice": "auto"})
            self.assertEqual(result["output_text"], "DEEPSEEK_TEXT_ONLY_VISIBLE")
            outbound = json.loads(open_.call_args.args[0].data.decode("utf-8"))
            self.assertNotIn("tools", outbound)
            self.assertIn("不能声称已经读取、修改、运行、提交或部署", outbound["messages"][0]["content"])

    def test_policy_round_trip_is_secret_free(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "policy.json"
            saved = save_policy(TEXT_ONLY_STRIP, path)
            self.assertEqual((saved["codex_tools_policy"], load_policy(path)), (TEXT_ONLY_STRIP, TEXT_ONLY_STRIP))
            self.assertNotIn("key", path.read_text(encoding="utf-8").lower())


if __name__ == "__main__":
    unittest.main()

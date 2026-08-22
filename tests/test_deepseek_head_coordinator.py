from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from codex_ai_router.context_collector import ContextCollector
from codex_ai_router.deepseek_head_coordinator import DeepSeekHeadCoordinator, choose_brain, classify_task
from codex_ai_router.local_agent import LocalAgent


class DeepSeekHeadCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "src").mkdir()
        (self.root / "src" / "router.py").write_text("# safe context\nTOKEN=secret-value\n", encoding="utf-8")
        self.storage = self.root / "agent"
        self.snapshot = lambda: [
            {"id": "deepseek-web-bridge", "type": "DEEPSEEK_WEB_BRIDGE", "enabled": True, "status": "ENABLED"},
            {"id": "local-model", "type": "LOCAL_MODEL", "enabled": True, "status": "ENABLED"},
        ]

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_difficulty_and_auto_selection(self):
        self.assertEqual(choose_brain("摘要这段文字", snapshot=self.snapshot())["selected_brain"], "local-light")
        self.assertEqual(classify_task("检查当前项目状态，不修改文件"), "read_only")
        self.assertEqual(classify_task("设计多文件 API 重构方案"), "complex")
        selected = choose_brain("设计多文件 API 重构方案", snapshot=self.snapshot())
        self.assertEqual(selected["selected_brain"], "deepseek-bridge-direct")
        self.assertIsNone(selected["fallback_reason"])

    def test_context_collection_is_bounded_readonly_and_redacted(self):
        collector = ContextCollector(self.root, self.storage / "ledger.jsonl")
        bundle = collector.collect("搜索 router 文件，不修改文件")
        self.assertEqual((bundle["collection_mode"], bundle["redaction_applied"], bundle["files_modified"]), ("read_only", True, "NO"))
        raw = json.dumps(bundle, ensure_ascii=False)
        self.assertNotIn("secret-value", raw)
        self.assertNotIn("prompt_response_logged", raw.lower().replace("prompt_response_logged", ""))
        self.assertIn("git status --short", bundle["commands_executed"])
        self.assertEqual(set(bundle["loopback_ports"]), {"8791", "8792", "8793", "18789"})

    def test_coordinate_plan_only_does_not_invoke_brain(self):
        coordinator = DeepSeekHeadCoordinator(self.root, LocalAgent(self.root, self.storage), provider_snapshot=self.snapshot)
        with patch("codex_ai_router.deepseek_head_coordinator.invoke_brain") as invoke:
            result = coordinator.coordinate({"task": "设计多文件 API 重构方案", "brain_provider": "auto"})
        invoke.assert_not_called()
        self.assertEqual((result["brain_invoked"], result["files_modified"], result["tools_forwarded"]), ("NO", "NO", "NO"))
        self.assertTrue(result["context_bundle_id"])
        self.assertIn("task_summary", result["agent_plan"])

    def test_deepseek_explicit_invocation_uses_text_only_and_normalizes_text(self):
        coordinator = DeepSeekHeadCoordinator(self.root, LocalAgent(self.root, self.storage), provider_snapshot=self.snapshot)
        with patch("codex_ai_router.deepseek_head_coordinator.invoke_brain", return_value="分析完成") as invoke:
            result = coordinator.coordinate({"task": "设计多文件 API 重构方案", "brain_provider": "deepseek-bridge-direct", "invoke_brain": True})
        invoke.assert_called_once()
        prompt = invoke.call_args.args[1]
        self.assertIn("不能调用工具", prompt)
        self.assertNotIn("tools", prompt.lower())
        self.assertEqual((result["brain_invoked"], result["agent_plan"]["brain_provider"], result["files_modified"]), ("YES", "deepseek-bridge-direct", "NO"))

    def test_external_context_forwarding_is_not_authorized(self):
        snapshot = lambda: [{"id": "ext", "type": "EXTERNAL_API_ALLOWED", "enabled": True, "status": "ENABLED"}]
        coordinator = DeepSeekHeadCoordinator(self.root, LocalAgent(self.root, self.storage), provider_snapshot=snapshot)
        result = coordinator.coordinate({"task": "分析日志", "brain_provider": "external-allowed", "invoke_brain": True})
        self.assertEqual(result["error_code"], "EXTERNAL_CONTEXT_FORWARDING_NOT_AUTHORIZED")
        self.assertEqual(result["files_modified"], "NO")

    def test_invalid_structured_brain_output_has_explicit_error(self):
        coordinator = DeepSeekHeadCoordinator(self.root, LocalAgent(self.root, self.storage), provider_snapshot=self.snapshot)
        with patch("codex_ai_router.deepseek_head_coordinator.invoke_brain", return_value='{"not_a_plan": true}'):
            result = coordinator.coordinate({"task": "设计多文件 API 重构方案", "brain_provider": "deepseek-bridge-direct", "invoke_brain": True})
        self.assertEqual(result["error_code"], "AGENT_PLAN_SCHEMA_INVALID")

    def test_high_risk_stops_before_brain_invocation(self):
        coordinator = DeepSeekHeadCoordinator(self.root, LocalAgent(self.root, self.storage), provider_snapshot=self.snapshot)
        with patch("codex_ai_router.deepseek_head_coordinator.invoke_brain") as invoke:
            result = coordinator.coordinate({"task": "删除所有文件并 git push", "brain_provider": "deepseek-bridge-direct", "invoke_brain": True})
        invoke.assert_not_called()
        self.assertEqual(result["error_code"], "LOCAL_AGENT_HIGH_RISK_STOP")


if __name__ == "__main__":
    unittest.main()

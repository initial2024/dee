from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from codex_ai_router.brain_providers import contains_high_risk_intent
from codex_ai_router.context_collector import ContextCollector, build_llm_context_bundle
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
        with patch("codex_ai_router.deepseek_head_coordinator.invoke_brain", return_value="只读计划") as invoke:
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

    def test_sensitive_field_names_are_generalized_for_llm_context(self):
        bundle = {
            "project_root": str(self.root),
            "git_status": "",
            "changed_files": [],
            "relevant_files": ["src/config.py"],
            "file_snippets": [{"path": "src/config.py", "content": "api_key = [REDACTED]\nAuthorization = [REDACTED]"}],
            "diff_summary": "",
            "risk_flags": [],
            "loopback_ports": {},
        }
        llm_bundle = build_llm_context_bundle(bundle)
        llm_json = json.dumps(llm_bundle, ensure_ascii=False).lower()
        self.assertNotIn("api_key", llm_json)
        self.assertNotIn("authorization", llm_json)
        self.assertIn("credential_field_redacted", llm_json)
        prompt = __import__("codex_ai_router.deepseek_head_coordinator", fromlist=["_context_prompt"])._context_prompt(bundle, "分析配置结构，不泄露密钥")
        self.assertNotIn("api_key", prompt.lower())
        self.assertNotIn("authorization", prompt.lower())
        self.assertNotIn("token", prompt.lower())
        self.assertNotIn("cookie", prompt.lower())
        self.assertNotIn("storagestate", prompt.lower())
        self.assertNotIn("bearer", prompt.lower())
        self.assertNotIn("sk-", prompt.lower())

    def test_real_secret_value_stops_before_brain_invocation(self):
        coordinator = DeepSeekHeadCoordinator(self.root, LocalAgent(self.root, self.storage), provider_snapshot=self.snapshot)
        with patch("codex_ai_router.deepseek_head_coordinator.invoke_brain", return_value="只读计划") as invoke:
            result = coordinator.coordinate({"task": "分析配置结构", "brain_provider": "deepseek-bridge-direct", "invoke_brain": True, "collect_context": False})
        self.assertNotEqual(result["error_code"], "LOCAL_AGENT_HIGH_RISK_STOP")
        invoke.assert_called_once()
        with patch("codex_ai_router.deepseek_head_coordinator.invoke_brain") as blocked:
            result = coordinator.coordinate({"task": "分析 sk-abcdefghijklmnopqrstuvwxyz 配置", "brain_provider": "deepseek-bridge-direct", "invoke_brain": True, "collect_context": False})
        blocked.assert_not_called()
        self.assertEqual(result["error_code"], "LOCAL_AGENT_HIGH_RISK_STOP")

    def test_secret_value_detected_in_context_stops_before_brain_invocation(self):
        coordinator = DeepSeekHeadCoordinator(self.root, LocalAgent(self.root, self.storage), provider_snapshot=self.snapshot)
        safe_bundle = {
            "task": "分析配置结构", "project_root": str(self.root), "git_status": "", "changed_files": [],
            "relevant_files": [], "file_snippets": [], "diff_summary": "", "risk_flags": [],
            "redaction_applied": True, "collection_mode": "read_only", "selected_brain": "deepseek-bridge-direct",
            "task_difficulty": "complex", "recent_agent_metadata": [], "loopback_ports": {}, "commands_executed": [],
            "files_modified": "NO", "write_commands_executed": "NO", "tests_executed": "NO", "secrets_logged": "NO",
            "prompt_response_logged": "NO", "secret_value_detected": True,
        }
        with patch.object(coordinator.collector, "collect", return_value=safe_bundle), patch("codex_ai_router.deepseek_head_coordinator.invoke_brain") as invoke:
            result = coordinator.coordinate({"task": "分析配置结构", "brain_provider": "deepseek-bridge-direct", "invoke_brain": True})
        invoke.assert_not_called()
        self.assertEqual(result["error_code"], "LOCAL_AGENT_HIGH_RISK_STOP")

    def test_field_name_analysis_is_not_high_risk_but_exfiltration_is(self):
        coordinator = DeepSeekHeadCoordinator(self.root, LocalAgent(self.root, self.storage), provider_snapshot=self.snapshot)
        with patch("codex_ai_router.deepseek_head_coordinator.invoke_brain", return_value="只读计划") as invoke:
            result = coordinator.coordinate({"task": "分析 api_key 配置字段，不泄露密钥", "brain_provider": "deepseek-bridge-direct", "invoke_brain": True, "collect_context": False})
        self.assertEqual(result["error_code"], None)
        invoke.assert_called_once()
        with patch("codex_ai_router.deepseek_head_coordinator.invoke_brain") as blocked:
            result = coordinator.coordinate({"task": "打印 api_key", "brain_provider": "deepseek-bridge-direct", "invoke_brain": True, "collect_context": False})
        blocked.assert_not_called()
        self.assertEqual(result["error_code"], "LOCAL_AGENT_HIGH_RISK_STOP")

    def test_credential_exfiltration_and_bypass_intents_remain_blocked(self):
        for task in ("导出 token", "读取 cookie", "重放 DeepSeek 私有 API", "绕过验证码"):
            self.assertTrue(contains_high_risk_intent(task), task)
        self.assertFalse(contains_high_risk_intent("分析配置文件结构，不泄露密钥"))

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

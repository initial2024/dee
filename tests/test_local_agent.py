from __future__ import annotations

import json
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from codex_ai_router.brain_providers import BrainProviderError, invoke_brain
from codex_ai_router.local_agent import LocalAgent, LocalAgentError, classify_risk, make_plan
from codex_ai_router.providers.base import ProviderError


class LocalAgentTests(unittest.TestCase):
    def test_risk_classification_and_high_risk_stop(self):
        self.assertEqual(classify_risk("只读检查 git 状态"), "low")
        self.assertEqual(classify_risk("修改一个 Python 文件并运行测试"), "medium")
        self.assertEqual(classify_risk("部署到生产并 git push"), "high")
        plan = make_plan("删除所有文件", "local-light")
        self.assertEqual((plan.deny_reason, plan.requires_write, plan.requires_tests, plan.requires_commit), ("LOCAL_AGENT_HIGH_RISK_STOP", False, False, False))

    def test_write_intent_respects_explicit_negation_and_patch_drafts(self):
        readonly_tasks = (
            "检查当前项目状态，不修改文件",
            "只读检查项目，不改文件",
            "搜索 local_agent，不写入",
            "分析但不修改当前代码",
            "生成 patch 草案但不应用",
        )
        for task in readonly_tasks:
            with self.subTest(task=task):
                plan = make_plan(task, "local-light")
                self.assertFalse(plan.requires_write)

        repair = make_plan("检查并修复 local_agent 的 bug", "local-light")
        self.assertTrue(repair.requires_write)
        self.assertTrue(any(step["requires_confirmation"] for step in repair.steps))

        apply_patch = make_plan("应用补丁", "local-light")
        self.assertTrue(apply_patch.requires_write)

        high_risk = make_plan("删除所有文件并提交", "local-light")
        self.assertEqual(high_risk.deny_reason, "LOCAL_AGENT_HIGH_RISK_STOP")

    def test_plan_is_structured_and_does_not_call_brain(self):
        with tempfile.TemporaryDirectory() as temp:
            agent = LocalAgent(Path(temp) / "repo", Path(temp) / "agent")
            result = agent.plan("分析当前项目并给出只读检查步骤", "deepseek-head")
            self.assertEqual((result["mode"], result["codex_agent_used"], result["auto_file_modify"]), ("PLAN_ONLY", "NO", "NO"))
            self.assertIn("task_summary", result)
            self.assertTrue((Path(result["plan_path"])).exists())
            raw = (Path(temp) / "agent" / "ledger.jsonl").read_text(encoding="utf-8")
            self.assertNotIn("Provide a read-only", raw)
            self.assertNotIn("prompt body", raw.lower())

    def test_brain_providers_are_explicit_and_mockable(self):
        class FakeLocal:
            def auto_ask(self, prompt, **_kwargs):
                self.prompt = prompt
                return "LOCAL_PLAN_OK"

        self.assertEqual(invoke_brain("local-light", "分析一个小任务", local_backend=FakeLocal()), "LOCAL_PLAN_OK")
        self.assertEqual(
            invoke_brain("deepseek-head", "分析一个小任务", deepseek_runner=lambda prompt, task_type: {"status": "PASS", "analysis": "DEEPSEEK_PLAN_OK"}),
            "DEEPSEEK_PLAN_OK",
        )
        self.assertEqual(
            invoke_brain("deepseek-bridge-direct", "分析一个小任务", deepseek_direct_runner=lambda prompt, task_type: {"status": "PASS", "analysis": "DIRECT_PLAN_OK"}),
            "DIRECT_PLAN_OK",
        )
        class FakeExternal:
            def ask(self, _prompt):
                return "EXTERNAL_PLAN_OK"

        self.assertEqual(invoke_brain("external-allowed", "分析一个小任务", external_factory=lambda: FakeExternal()), "EXTERNAL_PLAN_OK")

    def test_brain_error_mapping_preserves_original_code(self):
        class FailingLocal:
            def __init__(self, code):
                self.code = code

            def auto_ask(self, _prompt, **_kwargs):
                raise ProviderError(self.code)

        with self.assertRaises(BrainProviderError) as context:
            invoke_brain("local-light", "分析一个小任务", local_backend=FailingLocal("LOCAL_DIRECT_BACKEND_NOT_CONFIGURED"))
        self.assertEqual((context.exception.code, context.exception.metadata["original_error_code"]), ("LOCAL_BACKEND_NOT_CONFIGURED", "LOCAL_DIRECT_BACKEND_NOT_CONFIGURED"))

        with self.assertRaisesRegex(BrainProviderError, "LOCAL_MODEL_OFFLINE"):
            invoke_brain("local-light", "分析一个小任务", local_backend=FailingLocal("LLAMA_SERVER_NOT_FOUND"))
        with self.assertRaisesRegex(BrainProviderError, "LOCAL_MODEL_TIMEOUT"):
            invoke_brain("local-light", "分析一个小任务", local_backend=FailingLocal("COMPLETION_TIMEOUT"))

        class EmptyLocal:
            def auto_ask(self, _prompt, **_kwargs):
                return ""

        with self.assertRaisesRegex(BrainProviderError, "LOCAL_EMPTY_RESPONSE"):
            invoke_brain("local-light", "分析一个小任务", local_backend=EmptyLocal())

        with self.assertRaisesRegex(BrainProviderError, "DEEPSEEK_BRIDGE_OFFLINE"):
            invoke_brain("deepseek-head", "分析一个小任务", deepseek_runner=lambda _prompt, task_type: {"status": "BRIDGE_OFFLINE", "error_code": "BRIDGE_OFFLINE"})
        with self.assertRaisesRegex(BrainProviderError, "DEEPSEEK_LOGIN_REQUIRED"):
            invoke_brain("deepseek-head", "分析一个小任务", deepseek_runner=lambda _prompt, task_type: {"status": "UPSTREAM_HTTP_ERROR", "error_code": "UPSTREAM_HTTP_401"})
        with self.assertRaisesRegex(BrainProviderError, "DEEPSEEK_MODE_UNAVAILABLE"):
            invoke_brain("deepseek-bridge-direct", "分析一个小任务", deepseek_direct_runner=lambda _prompt, task_type: {"status": "DEEPSEEK_MODE_UNAVAILABLE", "error_code": "DEEPSEEK_MODE_UNAVAILABLE"})
        with patch("codex_ai_router.brain_providers.allowlisted_providers", return_value=[]):
            with self.assertRaisesRegex(BrainProviderError, "EXTERNAL_PROVIDER_NOT_ALLOWLIST_ENABLED"):
                invoke_brain("external-allowed", "分析一个小任务")
            with self.assertRaisesRegex(BrainProviderError, "NO_HEALTHY_BRAIN_PROVIDER"):
                invoke_brain("hybrid-agent", "分析一个小任务")

    def test_brain_error_original_code_is_exposed_as_safe_plan_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            agent = LocalAgent(Path(temp) / "repo", Path(temp) / "agent")
            with patch("codex_ai_router.local_agent.invoke_brain", side_effect=BrainProviderError("LOCAL_BACKEND_NOT_CONFIGURED", original_error_code="LOCAL_DIRECT_BACKEND_NOT_CONFIGURED")):
                result = agent.plan("检查当前项目状态，不修改文件", "local-light", invoke_brain_now=True)
        self.assertEqual(result["brain_error_code"], "LOCAL_BACKEND_NOT_CONFIGURED")
        self.assertEqual(result["metadata"], {"original_error_code": "LOCAL_DIRECT_BACKEND_NOT_CONFIGURED"})
        self.assertEqual((result["workspace_write"], result["prompt_response_logged"], result["secrets_logged"]), ("NO", "NO", "NO"))

    def test_explicit_brain_output_is_not_written_to_ledger(self):
        with tempfile.TemporaryDirectory() as temp:
            agent = LocalAgent(Path(temp) / "repo", Path(temp) / "agent")
            with patch("codex_ai_router.local_agent.invoke_brain", return_value="BRAIN_RESPONSE_SECRET"):
                result = agent.plan("分析一个小任务", "local-light", invoke_brain_now=True)
            self.assertEqual((result["brain_invoked"], result["brain_output"]), ("YES", "BRAIN_RESPONSE_SECRET"))
            raw = (Path(temp) / "agent" / "ledger.jsonl").read_text(encoding="utf-8")
            self.assertNotIn("BRAIN_RESPONSE_SECRET", raw)

    def test_brain_failure_is_safe_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            agent = LocalAgent(Path(temp) / "repo", Path(temp) / "agent")
            with patch("codex_ai_router.local_agent.invoke_brain", side_effect=BrainProviderError("NO_HEALTHY_BRAIN_PROVIDER")):
                result = agent.plan("分析一个小任务", "hybrid-agent", invoke_brain_now=True)
            self.assertEqual((result["brain_invoked"], result["brain_error_code"]), ("YES", "NO_HEALTHY_BRAIN_PROVIDER"))

    def test_external_brain_requires_allowlist_enabled(self):
        with patch("codex_ai_router.local_agent.allowlisted_providers", return_value=[]):
            with self.assertRaisesRegex(LocalAgentError, "EXTERNAL_PROVIDER_NOT_ALLOWLIST_ENABLED"):
                make_plan("分析日志", "external-allowed")
        with patch("codex_ai_router.local_agent.allowlisted_providers", return_value=[{"type": "EXTERNAL_API_ALLOWED", "enabled": True, "status": "ENABLED"}]):
            plan = make_plan("分析日志", "external-allowed")
        self.assertEqual((plan.brain_status, plan.brain_provider), ("ALLOWLIST_ENABLED_NOT_CALLED", "external-allowed"))

    def test_readonly_executes_only_two_read_commands_and_writes_no_workspace(self):
        calls = []

        def runner(args, _cwd, _timeout):
            calls.append(tuple(args))
            return 0, "safe output"

        with tempfile.TemporaryDirectory() as temp:
            root, storage = Path(temp) / "repo", Path(temp) / "agent"
            root.mkdir()
            result = LocalAgent(root, storage, runner=runner).readonly("只读检查")
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(calls, [("git", "status", "--short"), ("git", "diff", "--name-only")])
        self.assertEqual(result["workspace_write"], "NO")

    def test_draft_apply_test_and_commit_need_confirmation(self):
        with tempfile.TemporaryDirectory() as temp:
            root, storage = Path(temp) / "repo", Path(temp) / "agent"
            root.mkdir()
            agent = LocalAgent(root, storage, runner=lambda *_args: (0, ""))
            plan_id = agent.plan("修改代码并运行测试", "local-light")["plan_id"]
            for action in (
                lambda: agent.draft_patch(plan_id),
                lambda: agent.test(plan_id),
                lambda: agent.commit(plan_id, ["src/app.py"], "safe local commit"),
            ):
                with self.assertRaisesRegex(LocalAgentError, "CONFIRMATION_REQUIRED"):
                    action()
            patch_file = root / "candidate.patch"
            patch_file.write_text("# candidate\n", encoding="utf-8")
            with self.assertRaisesRegex(LocalAgentError, "CONFIRMATION_REQUIRED"):
                agent.apply(plan_id, str(patch_file))

    def test_confirmed_draft_is_not_applied_and_confirmed_test_uses_allowlist(self):
        calls = []

        def runner(args, _cwd, _timeout):
            calls.append(tuple(args))
            return 0, "PASS"

        with tempfile.TemporaryDirectory() as temp:
            root, storage = Path(temp) / "repo", Path(temp) / "agent"
            root.mkdir()
            agent = LocalAgent(root, storage, runner=runner)
            plan_id = agent.plan("修改代码并测试", "local-light")["plan_id"]
            draft = agent.draft_patch(plan_id, confirm=True)
            self.assertEqual((draft["status"], draft["applied"]), ("PATCH_DRAFTED", "NO"))
            tested = agent.test(plan_id, "git-diff-check", confirm=True)
            self.assertEqual((tested["status"], tested["test"]), ("PASS", "git-diff-check"))
            self.assertIn(("git", "diff", "--check"), calls)

    def test_apply_rejects_high_risk_patch_text(self):
        with tempfile.TemporaryDirectory() as temp:
            root, storage = Path(temp) / "repo", Path(temp) / "agent"
            root.mkdir()
            agent = LocalAgent(root, storage, runner=lambda *_args: (0, ""))
            plan_id = agent.plan("修改一个低风险文本文件", "local-light")["plan_id"]
            patch_file = root / "danger.patch"
            patch_file.write_text("# git push must never be executed\n", encoding="utf-8")
            with self.assertRaisesRegex(LocalAgentError, "LOCAL_AGENT_HIGH_RISK_STOP"):
                agent.apply(plan_id, str(patch_file), confirm=True)

    def test_dangerous_test_name_and_push_are_not_available(self):
        self.assertNotIn("git-push", LocalAgent.SAFE_TESTS)
        with tempfile.TemporaryDirectory() as temp:
            agent = LocalAgent(Path(temp) / "repo", Path(temp) / "agent")
            plan_id = agent.plan("提交一个安全修复", "local-light")["plan_id"]
            with self.assertRaisesRegex(LocalAgentError, "TEST_NOT_ALLOWLISTED"):
                agent.test(plan_id, "git-push", confirm=True)
            denied = agent.plan("git push 到公网", "local-light")
            self.assertEqual(denied["deny_reason"], "LOCAL_AGENT_HIGH_RISK_STOP")

    def test_credential_tasks_are_high_risk_stopped_before_brain_call(self):
        plan = make_plan("分析这个 API key 并输出 token", "deepseek-head")
        self.assertEqual(plan.deny_reason, "LOCAL_AGENT_HIGH_RISK_STOP")
        with self.assertRaisesRegex(BrainProviderError, "LOCAL_AGENT_HIGH_RISK_STOP"):
            invoke_brain("local-light", "分析这个 API key")

    def test_ledger_is_metadata_only(self):
        with tempfile.TemporaryDirectory() as temp:
            storage = Path(temp) / "agent"
            agent = LocalAgent(Path(temp) / "repo", storage)
            agent.plan("只回复 SECRET_TEST，不保存正文", "local-light")
            records = [json.loads(line) for line in (storage / "ledger.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(records[0]["saved_body"], False)
        self.assertNotIn("只回复 SECRET_TEST", json.dumps(records, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()

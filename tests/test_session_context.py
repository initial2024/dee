from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from codex_ai_router.deepseek_head_coordinator import DeepSeekHeadCoordinator
from codex_ai_router.local_agent import LocalAgent
from codex_ai_router.session_context import SessionContextError, SessionContextHub


class SessionContextHubTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        self.root.mkdir()
        (self.root / "src").mkdir()
        (self.root / "src" / "safe.py").write_text("VALUE = '[REDACTED]'\n", encoding="utf-8")
        self.hub = SessionContextHub(self.root)
        self.session = self.hub.create("本地补丁草案审查")
        self.session_id = self.session["task_session_id"]

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_create_has_stable_required_layout(self) -> None:
        directory = self.root / self.session["session_directory"]
        self.assertTrue(self.session_id.startswith("task-"))
        for name in ("session.json", "rolling_summary.md", "context_bundle.json", "llm_context_bundle.json", "codex_status.md", "deepseek_plan.md", "patch_draft_status.json", "decisions.md", "safety_flags.json"):
            self.assertTrue((directory / name).is_file(), name)
        self.assertEqual(self.hub.status(self.session_id)["task_session_id"], self.session_id)

    def test_append_summary_and_long_log_are_sanitized(self) -> None:
        result = self.hub.append_codex_status(self.session_id, "完成只读检查。" + ("日志片段 " * 500))
        self.assertEqual(result["prompt_response_logged"], "NO")
        rolling = (self.hub.path(self.session_id) / "rolling_summary.md").read_text(encoding="utf-8")
        self.assertLess(len(rolling), 2200)
        self.assertIn("完成只读检查", rolling)

    def test_sensitive_and_environment_references_are_rejected(self) -> None:
        source = self.root / ".env"
        source.write_text("VALUE=[REDACTED]", encoding="utf-8")
        with self.assertRaises(SessionContextError) as blocked:
            self.hub.append_codex_status_from_file(self.session_id, source)
        self.assertEqual(blocked.exception.code, "SESSION_STATUS_FILE_NOT_ALLOWED")
        with self.assertRaises(SessionContextError):
            self.hub.append_codex_status_from_file(self.session_id, Path(self.temp.name) / "outside.md")

    def test_llm_context_omits_prompt_response_and_sensitive_names(self) -> None:
        self.hub.append_codex_status(self.session_id, "执行状态正常；credential_field_redacted 已移除。")
        self.hub.store_context_bundles(self.session_id, {"task": "不保存", "file_snippets": [{"content": "credential_field_redacted=[REDACTED]"}]}, {"file_snippets": [{"content": "credential_field_redacted=[REDACTED]"}]})
        encoded = json.dumps(self.hub.llm_context(self.session_id), ensure_ascii=False).lower()
        for forbidden in ("api_key", "token", "cookie", "authorization", "storagestate", "storage_state", "prompt", "response"):
            self.assertNotIn(forbidden, encoded)
        self.assertIn("credential_field_redacted", encoded)

    def test_deepseek_outcome_tracks_unavailable_without_model_text(self) -> None:
        self.hub.record_deepseek_outcome(self.session_id, {"status": "ERROR", "error_code": "PATCH_DRAFT_UNAVAILABLE", "brain_invoked": "YES"})
        status = json.loads((self.hub.path(self.session_id) / "patch_draft_status.json").read_text(encoding="utf-8"))
        self.assertEqual((status["status"], status["patch_draft_applied"]), ("unavailable", False))
        self.assertIn("missing_context_summary", status)


class SessionCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        self.root.mkdir()
        (self.root / "src").mkdir()
        (self.root / "src" / "router.py").write_text("# context\n", encoding="utf-8")
        self.agent = LocalAgent(self.root, Path(self.temp.name) / "ledger")
        self.snapshot = lambda: [{"id": "deepseek-web-bridge", "type": "DEEPSEEK_WEB_BRIDGE", "enabled": True, "status": "ENABLED"}]
        self.coordinator = DeepSeekHeadCoordinator(self.root, self.agent, provider_snapshot=self.snapshot)
        self.session_id = self.coordinator.session_hub.create("session coordinator test")["task_session_id"]

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_session_bound_coordinate_is_plan_only_and_stores_safe_bundles(self) -> None:
        with patch("codex_ai_router.deepseek_head_coordinator.invoke_brain") as invoke:
            result = self.coordinator.coordinate({"task": "检查当前项目状态，不修改文件", "brain_provider": "auto", "task_session_id": self.session_id, "use_session_context": True})
        invoke.assert_not_called()
        self.assertEqual((result["task_session_id"], result["brain_invoked"]), (self.session_id, "NO"))
        directory = self.coordinator.session_hub.path(self.session_id)
        stored = json.loads((directory / "llm_context_bundle.json").read_text(encoding="utf-8"))
        encoded = json.dumps(stored, ensure_ascii=False).lower()
        self.assertNotIn("检查当前项目状态，不修改文件", encoded)
        self.assertNotIn("task\":", encoded)

    def test_session_context_requires_existing_id(self) -> None:
        with self.assertRaisesRegex(Exception, "TASK_SESSION_ID_REQUIRED"):
            self.coordinator.coordinate({"task": "只读检查", "use_session_context": True})


if __name__ == "__main__":
    unittest.main()

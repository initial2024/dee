from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from codex_ai_router.session_context import SessionContextHub
from codex_ai_router.work_profiles import (
    DEFAULT_WORK_PROFILES,
    REASONING_STRENGTH_ZH,
    classify_work_profile,
    confirm_codex_mode,
    generate_codex_handoff,
    select_work_profile,
)


class WorkProfileTests(unittest.TestCase):
    def test_profiles_have_required_axes(self) -> None:
        required = {
            "profile_id", "task_difficulty", "desired_reasoning", "codex_custom_mode",
            "codex_reasoning_strength", "deepseek_mode", "local_model_policy",
            "external_api_policy", "local_agent_policy", "search_policy", "vision_policy",
            "file_policy", "apply_policy", "test_policy", "commit_policy", "risk_level",
        }
        self.assertEqual(set(DEFAULT_WORK_PROFILES), {
            "simple_readonly", "medium_analysis", "medium_patch_draft", "complex_debug",
            "patch_review_high_risk", "official_codex_handoff",
        })
        for profile in DEFAULT_WORK_PROFILES.values():
            self.assertTrue(required.issubset(profile.as_dict()))

    def test_reasoning_strengths_have_chinese_labels(self) -> None:
        self.assertEqual(REASONING_STRENGTH_ZH["very_low"], "极低")
        self.assertEqual(REASONING_STRENGTH_ZH["max"], "最高")

    def test_classifier_maps_simple_medium_complex_and_high_risk(self) -> None:
        self.assertEqual(classify_work_profile("查看当前项目状态，只读"), "simple_readonly")
        self.assertEqual(classify_work_profile("为一个局部 bug 生成 patch 草案"), "medium_patch_draft")
        self.assertEqual(classify_work_profile("分析 test_v11 超时并检查 Router"), "complex_debug")
        self.assertEqual(classify_work_profile("打印 api key 并部署生产"), "patch_review_high_risk")

    def test_explicit_profile_and_official_handoff(self) -> None:
        profile = select_work_profile("任意任务", profile_id="official_codex_handoff")
        self.assertEqual(profile.target_executor, "codex_official")
        handoff = generate_codex_handoff(profile)
        self.assertTrue(handoff.startswith("请在 Codex 官方直连模式中继续执行。"))
        self.assertIn("官方工具", handoff)
        self.assertNotIn("Authorization", handoff)

    def test_custom_handoff_has_required_strength_instruction(self) -> None:
        profile = select_work_profile("生成 patch 草案", profile_id="medium_patch_draft")
        handoff = generate_codex_handoff(profile)
        self.assertTrue(handoff.startswith("请在 Codex 自定义模式中选择：推理强度=高。"))
        confirmed = confirm_codex_mode(profile, confirmed_mode=True, confirmed_strength="高")
        self.assertEqual(confirmed["user_confirmed_codex_mode"], "YES")
        self.assertEqual(confirmed["user_confirmed_codex_reasoning_strength"], "high")
        self.assertEqual(confirmed["codex_ui_scraping"], "NO")

    def test_external_is_disabled_and_internal_provider_not_public(self) -> None:
        for profile in DEFAULT_WORK_PROFILES.values():
            data = profile.as_dict()
            self.assertEqual(data["external_api_policy"], "disabled")
        # Work Profile output is local control metadata, not an API-only model list.
        self.assertNotIn("api-only", json.dumps([p.as_dict() for p in DEFAULT_WORK_PROFILES.values()]).lower())

    def test_session_records_only_profile_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            hub = SessionContextHub(root)
            session_id = hub.create("Work Profile 测试")['task_session_id']
            result = hub.append_work_profile(session_id, select_work_profile("只读状态").as_dict(), user_confirmed_codex_mode="YES", handoff_created_at="2026-08-30T00:00:00Z")
            self.assertEqual(result["user_confirmed_codex_mode"], "YES")
            stored = json.loads((hub.path(session_id) / "session.json").read_text(encoding="utf-8"))
            profile = stored["work_profile"]
            self.assertEqual(profile["recommended_codex_reasoning_strength"], "light")
            encoded = json.dumps(stored, ensure_ascii=False).lower()
            for forbidden in ("api_key", "token", "cookie", "authorization", "storage_state", "password"):
                self.assertNotIn(forbidden, encoded)


if __name__ == "__main__":
    unittest.main()

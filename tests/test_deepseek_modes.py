from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_ai_router.deepseek_modes import probe_deepseek_modes, select_deepseek_mode
from codex_ai_router.provider_allowlist import providers


class FakeResponse:
    def __init__(self, payload: dict, status: int = 200):
        self.payload = payload
        self.status = status

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def availability(*available: str) -> dict[str, dict[str, object]]:
    return {
        mode: {"status": "AVAILABLE" if mode in available else "UI_PROBE_FAILED", "controlDetected": True, "controllable": mode in available}
        for mode in ("normal", "search", "thinking", "expert")
    }


class DeepSeekModeTests(unittest.TestCase):
    def test_auto_prefers_search_for_current_or_latest_intent(self):
        result = select_deepseek_mode("请搜索今天的最新文档", availability=availability("normal", "search"))
        self.assertEqual(result["selected_model_alias"], "deepseek-web-search")
        self.assertEqual(result["selected_mode"], "search")

    def test_auto_prefers_thinking_then_expert_by_task_language(self):
        thinking = select_deepseek_mode("请做严谨分析和架构设计", availability=availability("normal", "thinking", "expert"))
        self.assertEqual(thinking["selected_mode"], "thinking")
        expert = select_deepseek_mode("请修复这段 Python 代码报错", availability=availability("normal", "expert"))
        self.assertEqual(expert["selected_mode"], "expert")

    def test_simple_task_falls_back_to_normal(self):
        result = select_deepseek_mode("只回复一句话", availability=availability("normal"))
        self.assertEqual(result["selected_model_alias"], "deepseek-web")
        self.assertTrue(result["mode_available"])

    def test_fixed_mode_has_priority_and_does_not_silently_fallback(self):
        result = select_deepseek_mode("请搜索最新消息", user_preference="expert", availability=availability("normal", "search"))
        self.assertEqual(result["selected_mode"], "expert")
        self.assertFalse(result["mode_available"])
        self.assertEqual(result["fallback_reason"], "DEEPSEEK_MODE_UNAVAILABLE")

    def test_fixed_preference_beats_explicit_model_alias(self):
        result = select_deepseek_mode(
            "普通问题",
            user_preference="thinking",
            explicit_model_alias="deepseek-web-search",
            availability=availability("thinking", "search"),
        )
        self.assertEqual(result["selected_mode"], "thinking")
        self.assertEqual(result["why_selected"], "用户手动固定模式")

    def test_legacy_aliases_remain_compatible(self):
        fast = select_deepseek_mode("普通问题", explicit_model_alias="deepseek-web-fast", availability=availability("normal"))
        head = select_deepseek_mode("普通问题", explicit_model_alias="deepseek-head", availability=availability("expert"))
        self.assertEqual(fast["selected_model_alias"], "deepseek-web-fast")
        self.assertEqual(head["selected_model_alias"], "deepseek-head")

    def test_unavailable_auto_target_reports_reason_when_normal_is_available(self):
        result = select_deepseek_mode("请搜索最新消息", availability=availability("normal"))
        self.assertEqual(result["selected_mode"], "normal")
        self.assertEqual(result["fallback_reason"], "SEARCH_MODE_UNAVAILABLE")

    def test_probe_is_read_only_and_persists_metadata_only(self):
        def opener(request, timeout=0):
            if request.full_url.endswith("/health"):
                return FakeResponse({"ok": True, "deepseek": {"pageReady": True, "loggedIn": True}})
            return FakeResponse({
                "ok": True,
                "promptSent": False,
                "clickSend": False,
                "modes": {
                    "normal": {"status": "AVAILABLE", "controlDetected": True, "controllable": True},
                    "search": {"status": "AVAILABLE", "controlDetected": True, "controllable": True},
                    "thinking": {"status": "UI_PROBE_FAILED", "controlDetected": True, "controllable": False},
                    "expert": {"status": "AVAILABLE", "controlDetected": True, "controllable": True},
                },
            })

        with tempfile.TemporaryDirectory() as temp, patch("codex_ai_router.deepseek_modes.urlopen", side_effect=opener):
            state_path = Path(temp) / "probe.json"
            result = probe_deepseek_modes(state_path=state_path)
            self.assertEqual(result["status"], "PASS")
            self.assertFalse(result["promptSent"])
            self.assertFalse(result["clickSend"])
            self.assertEqual(result["modes"]["search"]["status"], "AVAILABLE")
            stored = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertNotIn("只回复", json.dumps(stored, ensure_ascii=False))
            self.assertNotIn("authorization", json.dumps(stored, ensure_ascii=False).lower())

    def test_probe_pass_without_controllable_modes_does_not_mark_provider_ready(self):
        with patch("codex_ai_router.provider_allowlist._health", side_effect=[("ENABLED", {}), ("OFFLINE", {})]), patch("codex_ai_router.provider_allowlist.bridge_api_key_present", return_value=True), patch("codex_ai_router.provider_allowlist.load_probe_state", return_value={"status": "PASS", "modes": availability()}):
            record = providers()[0]
        self.assertEqual(record["status"], "UI_PROBE_FAILED")
        self.assertEqual(record["models"], [])


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_ai_router.deepseek_modes import escalation_target, preflight_attachment, probe_deepseek_modes, select_deepseek_mode


def availability(*names: str):
    return {name: {"status": "AVAILABLE" if name in names else "UNAVAILABLE", "controlDetected": True, "controllable": name in names} for name in ("quick", "expert", "thinking", "search", "vision", "file")}


class FakeResponse:
    def __init__(self, value: dict): self.value, self.status = value, 200
    def read(self):
        import json
        return json.dumps(self.value).encode("utf-8")
    def __enter__(self): return self
    def __exit__(self, *_): return False


class DeepSeekModeTests(unittest.TestCase):
    def test_project_complex_work_chooses_expert_but_small_ui_chooses_quick_thinking(self):
        for task in ("分析 test_v11 本地流式 HTTP 超时", "修复 Patch Synthesizer 协议兼容问题"):
            result = select_deepseek_mode(task, availability=availability("expert", "thinking", "quick"))
            self.assertEqual(result["selected_mode"], "expert_thinking")
            self.assertEqual(result["smart_search"], "OFF")
        ui = select_deepseek_mode("修复单个 UI 布局文字截断", availability=availability("quick", "thinking"))
        self.assertEqual(ui["selected_mode"], "quick_thinking")

    def test_image_with_explicit_attachment_chooses_vision(self):
        result = select_deepseek_mode("分析 UI 截图", image_path="C:/tmp/shot.png", availability=availability("expert", "thinking", "vision"))
        self.assertEqual(result["selected_mode"], "vision_expert_thinking")
        self.assertTrue(result["mode_available"])

    def test_search_simple_and_file_matrix(self):
        search = select_deepseek_mode("搜索 DeepSeek 最新模式分类", availability=availability("quick", "search"))
        simple = select_deepseek_mode("简单总结一句话", availability=availability("quick"))
        file = select_deepseek_mode("提取 PDF 附件文本", attachment_id="user-confirmed", availability=availability("expert", "thinking", "file"))
        self.assertEqual(search["selected_mode"], "quick_search")
        self.assertEqual(simple["selected_mode"], "quick_plain")
        self.assertEqual(file["selected_mode"], "file_extract")

    def test_complex_external_search_and_one_step_escalation(self):
        result = select_deepseek_mode("搜索最新 DeepSeek Router 架构兼容文档", availability=availability("expert", "thinking", "search"))
        self.assertEqual(result["selected_mode"], "expert_thinking_search")
        upgrade = escalation_target("quick_thinking", reason="DEEPSEEK_EMPTY_RESPONSE", difficulty="medium")
        self.assertEqual(upgrade["selected_mode"], "expert_thinking")
        self.assertEqual(upgrade["max_auto_escalation"], 1)

    def test_manual_mode_wins_and_never_silently_falls_back(self):
        result = select_deepseek_mode("搜索最新消息", user_preference="expert_thinking", availability=availability("quick"))
        self.assertEqual(result["selected_mode"], "expert_thinking")
        self.assertFalse(result["mode_available"])
        self.assertEqual(result["fallback_reason"], "DEEPSEEK_EXPERT_MODE_UNAVAILABLE")

    def test_explicit_base_alias_keeps_concrete_quick_profile(self):
        result = select_deepseek_mode("简单总结一句话", explicit_model_alias="deepseek-web", availability=availability("quick"))
        self.assertEqual(result["selected_mode"], "quick_plain")

    def test_screenshot_without_attachment_needs_image(self):
        result = select_deepseek_mode("分析 UI 截图", availability=availability("expert", "thinking", "vision"))
        self.assertEqual(result["selected_mode"], "vision_expert_thinking")
        self.assertFalse(result["mode_available"])
        self.assertEqual(result["fallback_reason"], "DEEPSEEK_VISION_UNAVAILABLE")

    def test_attachment_preflight_blocks_sensitive_and_never_uploads(self):
        with tempfile.TemporaryDirectory() as temp:
            blocked = Path(temp) / ".env"; blocked.write_text("x", encoding="utf-8")
            image = Path(temp) / "shot.png"; image.write_bytes(b"png")
            self.assertFalse(preflight_attachment(blocked)["upload_allowed"])
            self.assertTrue(preflight_attachment(image)["upload_allowed"])
            self.assertTrue(preflight_attachment(image)["requires_user_confirm"])

    def test_probe_is_get_only_and_keeps_mode_metadata(self):
        def opener(request, timeout=0):
            self.assertEqual(request.get_method(), "GET")
            if request.full_url.endswith("/health"): return FakeResponse({"ok": True})
            return FakeResponse({"modes": availability("quick", "expert", "thinking"), "quick_available": True, "expert_available": True, "thinking_available": True, "search_available": False, "vision_available": False, "file_upload_available": False, "current_base_mode": "quick", "current_thinking": False, "current_search": False, "current_modality": "text", "ui_changed": False, "login_required": False, "captcha_required": False})
        with tempfile.TemporaryDirectory() as temp, patch("codex_ai_router.deepseek_modes.urlopen", side_effect=opener):
            result = probe_deepseek_modes(state_path=Path(temp) / "probe.json")
        self.assertEqual(result["status"], "PASS")
        self.assertFalse(result["promptSent"]); self.assertFalse(result["clickSend"]); self.assertFalse(result["uploadAttempted"])


if __name__ == "__main__": unittest.main()

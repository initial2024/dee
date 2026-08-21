from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_ai_router.providers.local_backend import GGUFModel, ManagedLlamaCppBackend, discover_gguf_models
from codex_ai_router.providers.local_model_selector import LocalModelSelector, profile_from_model


class LocalSelectorTests(unittest.TestCase):
    def profiles(self):
        root = Path(tempfile.mkdtemp())
        models = [
            GGUFModel(root / "Qwen2.5-7B-Instruct-Q4_K_M.gguf", "qwen", 1, quantization="Q4_K_M"),
            GGUFModel(root / "L3-8B-Stheno-Q4_K_S.gguf", "stheno", 1, quantization="Q4_K_S"),
            GGUFModel(root / "Qwen3-14B-Q4_K_M.gguf", "big", 1, quantization="Q4_K_M"),
            GGUFModel(root / "mmproj-Qwen3-BF16.gguf", "mmproj", 1, quantization="F16", vision_projector=True, text_model=False),
            GGUFModel(root / "heavy-7B-BF16.gguf", "bf16", 1, quantization="BF16"),
        ]
        return [profile_from_model(item) for item in models]

    def test_profiles_exclude_mmproj_as_text(self):
        profile = next(item for item in self.profiles() if item["model_id"] == "mmproj")
        self.assertEqual(profile["text_model"], "NO")
        self.assertEqual(profile["vision_projector"], "YES")

    def test_simple_task_selects_instruct_and_skips_heavy(self):
        result = LocalModelSelector(self.profiles()).select("解释这个 Python 报错，不修改文件")
        self.assertEqual(result["selected_model"], "qwen")
        reasons = {item["reason"] for item in result["skipped_models"]}
        self.assertIn("MMPROJ_NOT_TEXT_MODEL", reasons)
        self.assertIn("BF16_AUTO_DISABLED", reasons)
        self.assertIn("HEAVY_MODEL_REQUIRES_HIGH_THRESHOLD", reasons)

    def test_roleplay_selects_creative(self):
        result = LocalModelSelector(self.profiles()).select("写一段角色扮演开场", mode="roleplay")
        self.assertEqual(result["selected_model"], "stheno")

    def test_complex_and_high_risk_stop_or_require_escalation(self):
        complex_result = LocalModelSelector(self.profiles()).select("复杂多文件重构", risk="complex")
        self.assertTrue(complex_result["requires_api_or_official_codex"])
        high_result = LocalModelSelector(self.profiles()).select("production deploy", risk="high")
        self.assertEqual(high_result["error_code"], "LOCAL_HIGH_RISK_SAFE_STOP")

    def test_manual_policy_priority(self):
        profiles = self.profiles()
        result = LocalModelSelector(profiles, policy={"manual_disabled_models": ["qwen"], "manual_only_model": "stheno"}).select("解释错误")
        self.assertEqual(result["selected_model"], "stheno")
        denied = LocalModelSelector(profiles, policy={"manual_disabled_models": ["stheno"], "manual_only_model": "stheno"}).select("解释错误")
        self.assertIsNone(denied["selected_model"])

    def test_config_discovery_marks_mmproj(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "mmproj-Qwen-BF16.gguf").write_bytes(b"x")
            found = discover_gguf_models([root])
            self.assertEqual(found[0].text_model, False)
            self.assertEqual(found[0].vision_projector, True)


if __name__ == "__main__":
    unittest.main()

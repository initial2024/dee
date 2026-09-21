from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_ai_router.providers.local_backend import (
    BONSAI_MINIMAL_SMOKE,
    PRISM_RUNTIME_ID,
    STANDARD_RUNTIME_ID,
    GGUFModel,
    ManagedLlamaCppBackend,
    discover_gguf_models,
    classify_bonsai_performance,
    prism_bonsai_runtime_status,
    runtime_for_model,
)
from codex_ai_router.providers.local_model_selector import LocalModelSelector, bonsai_format_from_filename, profile_from_model


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

    def test_bonsai_filename_formats_are_profiled_and_not_auto_selected(self):
        root = Path(tempfile.mkdtemp())
        bonsai = GGUFModel(root / "Ternary-Bonsai-7B-Instruct-PTQ1_0.gguf", "bonsai", 1, quantization="UNKNOWN")
        qwen = GGUFModel(root / "Qwen2.5-7B-Instruct-Q4_K_M.gguf", "qwen", 1, quantization="Q4_K_M")
        profile = profile_from_model(bonsai)
        self.assertEqual((profile["family_guess"], profile["bonsai_model"], profile["bonsai_format"], profile["quantization"]), ("Ternary-Bonsai", "YES", "PTQ1_0", "PTQ1_0"))
        result = LocalModelSelector([profile, profile_from_model(qwen)]).select("解释这个错误")
        self.assertEqual(result["selected_model"], "qwen")
        self.assertIn({"model": "bonsai", "reason": "BONSAI_REQUIRES_EXPLICIT_COMPATIBILITY_CHECK"}, result["skipped_models"])

    def test_all_reserved_bonsai_filename_formats_are_recognized(self):
        cases = {
            "Bonsai-Q2_0_g64.gguf": "Q2_0_G64",
            "Ternary-Bonsai-PQ2_0.gguf": "PQ2_0",
            "Ternary-Bonsai-PTQ1_0.gguf": "PTQ1_0",
            "Bonsai-Q1_0.gguf": "Q1_0",
        }
        for filename, expected in cases.items():
            self.assertEqual(bonsai_format_from_filename(filename), expected)

    def test_runtime_routing_requires_prism_for_bonsai_and_preserves_standard(self):
        root = Path(tempfile.mkdtemp())
        standard = GGUFModel(root / "Qwen-Q4_K_M.gguf", "standard", 1, quantization="Q4_K_M")
        ternary = GGUFModel(root / "Ternary-Bonsai-PQ2_0.gguf", "ternary", 1, quantization="PQ2_0")
        projector = GGUFModel(root / "mmproj-Bonsai.gguf", "projector", 1, quantization="UNKNOWN", text_model=False, vision_projector=True)
        self.assertEqual(runtime_for_model(standard)[0], STANDARD_RUNTIME_ID)
        self.assertEqual(runtime_for_model(ternary)[0], PRISM_RUNTIME_ID)
        self.assertEqual(runtime_for_model(projector), (None, "MMPROJ_NOT_TEXT_MODEL"))

    def test_uninstalled_prism_runtime_is_unknown_and_never_ready_for_download(self):
        root = Path(tempfile.mkdtemp()) / "prism-bonsai-runtime"
        status = prism_bonsai_runtime_status({"runtimes": {PRISM_RUNTIME_ID: {"runtime_path": str(root)}}})
        self.assertEqual(status["runtime_installed"], "NO")
        self.assertEqual(status["compatibility_status"], "BONSAI_RUNTIME_UNKNOWN")
        self.assertEqual(status["NO_IMPLICIT_RUNTIME_BUILD"], "YES")

    def test_bonsai_minimal_smoke_is_bounded_and_never_auto_enabled(self):
        self.assertLessEqual(BONSAI_MINIMAL_SMOKE["max_tokens"], 8)
        self.assertEqual(BONSAI_MINIMAL_SMOKE["temperature"], 0)
        classification, policy = classify_bonsai_performance("PASS", 56.223)
        self.assertEqual(classification, "BONSAI_USABLE_FAST")
        self.assertIn("Manual", policy)

    def test_bonsai_timeout_is_classified_without_auto_selection(self):
        classification, policy = classify_bonsai_performance("TIMEOUT", 300, "COMPLETION_TIMEOUT")
        self.assertEqual(classification, "BONSAI_LOADS_BUT_TOO_SLOW")
        self.assertIn("automatic selection", policy)

    def test_warm_model_and_measured_speed_affect_local_routing(self):
        profiles = self.profiles()
        qwen = next(item for item in profiles if item["model_id"] == "qwen")
        qwen["measured_generation_tps"] = 0.919
        result = LocalModelSelector(profiles, current_model="stheno").select("解释这个错误")
        self.assertEqual(result["selected_model"], "stheno")
        self.assertEqual(result["keep_warm_model"], "YES")
        self.assertEqual(result["model_switch_cost_accounted"], "YES")


if __name__ == "__main__":
    unittest.main()

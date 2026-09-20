from __future__ import annotations

import unittest

from codex_ai_router.deepseek_modes import DeepSeekCapabilityProfile, best_available_reasoning_profile, capability_profile, reasoning_capability_text, select_deepseek_mode
from codex_ai_router.work_profiles import get_work_profile, map_profile


def available(*names: str, combo: bool | None = None) -> dict:
    result = {name: {"status": "AVAILABLE" if name in names else "UNAVAILABLE", "controlDetected": True, "controllable": name in names} for name in ("quick", "expert", "thinking", "search", "vision", "file")}
    if combo is not None:
        result["search_combo_supported"] = combo
    result["ui_generation"] = "three_in_one"
    return result


def binary_available(*names: str) -> dict:
    result = available(*names)
    result.update({
        "reasoning_axis_type": "binary_toggle", "available_reasoning_strengths": ["off", "medium"],
        "max_available_reasoning_strength": "medium", "high_reasoning_supported": False,
        "max_reasoning_supported": False, "reasoning_strength_options_status": "binary",
    })
    return result


class DeepSeekThreeInOneTests(unittest.TestCase):
    def test_binary_capability_reports_medium_best_profile_and_dynamic_text(self):
        profile = best_available_reasoning_profile(binary_available("thinking", "search"))
        self.assertEqual(profile.reasoning_strength, "medium")
        self.assertEqual(profile.search_mode, "off")
        self.assertEqual(profile.available_reasoning_strengths, ("off", "medium"))
        self.assertFalse(profile.high_reasoning_supported)
        self.assertFalse(profile.max_reasoning_supported)
        self.assertIn("二值", reasoning_capability_text(binary_available("thinking", "search")))

    def test_binary_hard_profiles_never_silently_fallback(self):
        high = select_deepseek_mode("复杂 router 调试", availability=binary_available("thinking", "search"))
        self.assertEqual(high["selected_mode"], "best_available_reasoning")
        self.assertEqual(high["reasoning_strength"], "medium")
        self.assertEqual(high["smart_search"], "OFF")
        review = select_deepseek_mode("高风险 密钥 patch review", availability=binary_available("thinking", "search"))
        self.assertEqual(review["selected_mode"], "expert_max_review")
        self.assertFalse(review["mode_available"])
        self.assertEqual(review["error_code"], "PATCH_REVIEW_MAX_REASONING_UNAVAILABLE")

    def test_binary_manual_high_is_hard_unavailable(self):
        result = select_deepseek_mode("复杂 router 调试", user_preference="expert_thinking", availability=binary_available("thinking", "search"))
        self.assertEqual(result["selected_mode"], "expert_thinking")
        self.assertFalse(result["mode_available"])
        self.assertEqual(result["error_code"], "DEEPSEEK_REASONING_STRENGTH_UNAVAILABLE")

    def test_profile_contains_three_axes(self):
        value = capability_profile("expert_max_review", ui_generation="three_in_one")
        self.assertIsInstance(value, DeepSeekCapabilityProfile)
        self.assertEqual(value.reasoning_strength, "max")
        self.assertEqual(value.search_mode, "off")
        self.assertEqual(value.ui_generation, "three_in_one")

    def test_low_medium_high_max_selector_profiles(self):
        cases = (
            ("简单只读", "off"),
            ("单文件分析任务", "medium"),
            ("复杂 router 调试", "high"),
        )
        for text, strength in cases:
            result = select_deepseek_mode(text, availability=available("quick", "expert", "thinking"))
            self.assertEqual(result["reasoning_strength"], strength if strength != "off" else "low")
        review = select_deepseek_mode("高风险 密钥 patch review", availability=available("expert", "thinking"))
        self.assertEqual(review["selected_mode"], "expert_max_review")

    def test_complex_search_exposes_split_instead_of_silent_combo(self):
        result = select_deepseek_mode("搜索最新架构并进行复杂分析", availability=available("quick", "expert", "thinking", "search", combo=False))
        self.assertEqual(result["fallback_reason"], "DEEPSEEK_THREE_IN_ONE_COMBO_UNAVAILABLE")
        self.assertEqual(result["error_code"], "DEEPSEEK_SEARCH_COMBO_UNAVAILABLE")
        self.assertEqual(result["split_strategy"], {"first": "quick_search", "then": "expert_thinking"})
        self.assertEqual(result["combo_policy"], "allow_split_search_then_reason")

    def test_work_profile_keeps_codex_and_deepseek_strength_separate(self):
        profile = get_work_profile("patch_review_high_risk")
        self.assertEqual(profile.codex_reasoning_strength, "very_high")
        self.assertEqual(profile.deepseek_reasoning_strength, "max")
        mapped = map_profile(profile)
        self.assertEqual(mapped["deepseek"]["reasoning_strength"], "max")

    def test_three_in_one_aliases_use_capability_profiles_without_base_axis(self):
        for alias, expected, strength in (
            ("quick_thinking", "medium_reasoning", "medium"),
            ("expert_thinking", "high_reasoning", "high"),
            ("expert_max_review", "max_reasoning_review", "max"),
            ("quick_search", "medium_search", "medium"),
            ("expert_thinking_search", "high_search", "high"),
        ):
            value = capability_profile(alias, ui_generation="three_in_one")
            self.assertEqual((value.profile_id, value.reasoning_strength), (expected, strength))
            self.assertEqual(value.base_model_family, "web_default")

    def test_three_in_one_selection_does_not_require_quick_or_expert(self):
        result = select_deepseek_mode(
            "复杂 router 调试",
            availability={"ui_generation": "three_in_one", "thinking": {"status": "AVAILABLE"}, "search": {"status": "AVAILABLE"}},
        )
        self.assertTrue(result["mode_available"])
        self.assertEqual(result["selected_profile"]["profile_id"], "high_reasoning")


if __name__ == "__main__":
    unittest.main()

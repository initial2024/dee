from __future__ import annotations

import unittest

from codex_ai_router.deepseek_modes import DeepSeekCapabilityProfile, capability_profile, select_deepseek_mode
from codex_ai_router.work_profiles import get_work_profile, map_profile


def available(*names: str, combo: bool | None = None) -> dict:
    result = {name: {"status": "AVAILABLE" if name in names else "UNAVAILABLE", "controlDetected": True, "controllable": name in names} for name in ("quick", "expert", "thinking", "search", "vision", "file")}
    if combo is not None:
        result["search_combo_supported"] = combo
    result["ui_generation"] = "three_in_one"
    return result


class DeepSeekThreeInOneTests(unittest.TestCase):
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
            self.assertEqual(result["reasoning_strength"], strength if strength != "off" else "off")
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


if __name__ == "__main__":
    unittest.main()

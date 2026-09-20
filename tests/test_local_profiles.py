from __future__ import annotations

import unittest

from codex_ai_router.providers.local_profiles import build_history_chongzhen_prompt, get_local_profile


class LocalProfileTests(unittest.TestCase):
    def test_history_chongzhen_profile_has_safe_boundary(self):
        profile = get_local_profile("history_chongzhen")
        self.assertEqual(profile["selector_mode"], "roleplay")
        self.assertIn("史实", profile["fact_fiction_boundary"])
        self.assertIn("虚构", profile["fact_fiction_boundary"])
        self.assertEqual(profile["control_panel_scope"], "仅本地 smoke/demo，不是正式任务入口。")

    def test_history_prompt_requires_fact_fiction_labels(self):
        prompt = build_history_chongzhen_prompt("模拟军饷决策")
        self.assertIn("【史实】", prompt)
        self.assertIn("【推演】", prompt)
        self.assertIn("不得把历史模拟转换为现实政治建议", prompt)

    def test_history_prompt_rejects_empty_input(self):
        with self.assertRaisesRegex(ValueError, "HISTORY_PROFILE_PROMPT_REQUIRED"):
            build_history_chongzhen_prompt("  \n")

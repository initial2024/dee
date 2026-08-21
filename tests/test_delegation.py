import unittest

from codex_ai_router.delegation import explain_delegation


class DelegationTests(unittest.TestCase):
    def test_simple_task_allows_readonly_delegation(self):
        result = explain_delegation("summarize this README")
        self.assertEqual(result["risk"], "SIMPLE_READ")
        self.assertTrue(result["delegation_allowed"])
        self.assertFalse(result["write_allowed_default"])

    def test_chinese_readonly_summary_is_simple(self):
        result = explain_delegation("用一句话说明项目用途，不修改文件")
        self.assertEqual(result["risk"], "SIMPLE_READ")
        self.assertTrue(result["delegation_allowed"])

    def test_medium_code_is_advisory(self):
        result = explain_delegation("write a small function and tests")
        self.assertEqual(result["risk"], "MEDIUM_CODE")
        self.assertEqual(result["recommended_route"], "ROUTER_ADVISORY")

    def test_high_risk_stays_official(self):
        result = explain_delegation("deploy a production authentication change")
        self.assertEqual(result["risk"], "HIGH_RISK")
        self.assertFalse(result["delegation_allowed"])
        self.assertTrue(result["requires_official_codex"])

    def test_explicit_high_override_wins(self):
        result = explain_delegation("summarize this", risk_override="high")
        self.assertEqual(result["risk"], "HIGH_RISK")
        self.assertFalse(result["delegation_allowed"])

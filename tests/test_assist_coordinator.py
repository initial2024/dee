from __future__ import annotations

import unittest

from codex_ai_router.assist_coordinator import (
    ASSIST_STARTUP_INSTRUCTION,
    AssistCoordinator,
    AssistCoordinatorError,
    OFFICIAL_ASSISTED_COORDINATOR,
)


class AssistCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.coordinator = AssistCoordinator(provider_snapshot=lambda: [
            {"id": "local-light", "type": "LOCAL_MODEL", "enabled": True, "status": "ENABLED"},
            {"id": "deepseek-bridge-direct", "type": "DEEPSEEK_WEB_BRIDGE", "enabled": True, "status": "ENABLED"},
        ])

    def test_readonly_task_stays_local_and_does_not_invoke_brain(self):
        result = self.coordinator.coordinate({"task": "检查当前项目状态，不修改文件", "mode": "official_assisted"})
        self.assertEqual(result["mode"], OFFICIAL_ASSISTED_COORDINATOR)
        self.assertEqual(result["recommended_brain"], "local-light")
        self.assertTrue(result["local_agent_steps"])
        self.assertEqual(result["codex_required_steps"], [])
        self.assertEqual((result["requires_official_codex"], result["brain_invoked"], result["workspace_write"]), (False, "NO", "NO"))

    def test_complex_write_task_is_handed_to_official_codex(self):
        result = self.coordinator.coordinate({"task": "修改多个文件并运行测试", "mode": "official_assisted"})
        self.assertTrue(result["codex_required_steps"])
        self.assertTrue(result["requires_official_codex"])
        self.assertIn("CONFIRMATION_REQUIRED", result["stop_conditions"])

    def test_high_risk_is_stopped(self):
        result = self.coordinator.coordinate({"task": "删除所有文件并 git push", "mode": "official_assisted"})
        self.assertEqual(result["stop_conditions"], ["LOCAL_AGENT_HIGH_RISK_STOP"])
        self.assertEqual(result["local_agent_steps"], [])
        self.assertEqual(result["codex_agent_auto_invoked"], "NO")

    def test_unhealthy_brains_have_explicit_fallback(self):
        result = AssistCoordinator(provider_snapshot=lambda: []).coordinate({"task": "只读检查项目"})
        self.assertEqual((result["recommended_brain"], result["fallback_reason"]), ("manual-plan", "NO_HEALTHY_BRAIN_PROVIDER"))

    def test_limits_and_mode_are_validated(self):
        with self.assertRaisesRegex(AssistCoordinatorError, "INVALID_MAX_LOCAL_STEPS"):
            self.coordinator.coordinate({"task": "查看状态", "max_local_steps": 6})
        with self.assertRaisesRegex(AssistCoordinatorError, "ASSIST_MODE_INVALID"):
            self.coordinator.coordinate({"task": "查看状态", "mode": "custom_router"})

    def test_startup_instruction_is_fixed_and_safe(self):
        self.assertIn("127.0.0.1:18789/assist/coordinate", ASSIST_STARTUP_INSTRUCTION)
        self.assertIn("codex_required_steps", ASSIST_STARTUP_INSTRUCTION)
        self.assertIn("不要执行", ASSIST_STARTUP_INSTRUCTION)
        self.assertNotIn("Authorization", ASSIST_STARTUP_INSTRUCTION)


if __name__ == "__main__":
    unittest.main()

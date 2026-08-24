from __future__ import annotations

import unittest

from codex_ai_router.brain_providers import BrainProviderError, invoke_brain


class BrainProviderPreSendErrorTests(unittest.TestCase):
    def test_direct_mode_error_is_preserved_with_no_send_metadata(self):
        seen = {}

        def runner(_task, **kwargs):
            seen.update(kwargs)
            return {
                "status": "DEEPSEEK_BRIDGE_DIRECT_MODE_PARAM_INVALID",
                "error_code": "DEEPSEEK_BRIDGE_DIRECT_MODE_PARAM_INVALID",
                "provider_error_stage": "before_bridge_send",
                "bridge_send_attempted": "NO",
                "bridge_ui_send_attempt_count": 0,
                "model_output_available": "NO",
            }

        with self.assertRaises(BrainProviderError) as raised:
            invoke_brain("deepseek-bridge-direct", "只生成计划", deepseek_direct_runner=runner, selected_mode="expert_thinking", search=False)
        self.assertEqual(raised.exception.code, "DEEPSEEK_BRIDGE_DIRECT_MODE_PARAM_INVALID")
        self.assertEqual(raised.exception.metadata["provider_error_stage"], "before_bridge_send")
        self.assertEqual(raised.exception.metadata["bridge_send_attempted"], "NO")
        self.assertEqual((seen["selected_mode"], seen["search"]), ("expert_thinking", False))

    def test_unexpected_direct_wrapper_failure_is_sanitized_as_pre_send(self):
        def runner(_task, **_kwargs):
            raise TypeError("internal detail must not escape")

        with self.assertRaises(BrainProviderError) as raised:
            invoke_brain("deepseek-bridge-direct", "只生成计划", deepseek_direct_runner=runner)
        self.assertEqual(raised.exception.code, "PROVIDER_PRE_SEND_ERROR")
        self.assertEqual(raised.exception.original_error_code, "DEEPSEEK_BRIDGE_DIRECT_REQUEST_NOT_SENT")
        self.assertEqual((raised.exception.metadata["provider_error_stage"], raised.exception.metadata["bridge_send_attempted"]), ("before_bridge_send", "NO"))


if __name__ == "__main__":
    unittest.main()

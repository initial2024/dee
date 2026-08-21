from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_ai_router.groq_diagnostics import _live_error, diagnose, live_smoke
from codex_ai_router.providers.base import ProviderError
from codex_ai_router.providers.runtime_models import RuntimeModelState


def entry(**overrides):
    value = {
        "enabled": True,
        "allowlisted": False,
        "api_key_env": "GROQ_TEST_KEY",
        "model_registry": {"ALLOWED_MODELS": ["confirmed-model"]},
    }
    value.update(overrides)
    return value


class GroqEligibilityTests(unittest.TestCase):
    def state_with_pass(self) -> RuntimeModelState:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        state = RuntimeModelState(Path(temp.name) / "runtime.json")
        state.record("groq-2", "confirmed-model", "PASS", 0.1)
        return state

    def test_legacy_enabled_without_allowlist_is_explicitly_gated(self):
        result = diagnose("groq-2", entry(), self.state_with_pass(), {"GROQ_TEST_KEY": "secret"})
        self.assertTrue(result["legacy_enabled"])
        self.assertFalse(result["allowlist_enabled"])
        self.assertFalse(result["live_request_allowed"])
        self.assertEqual(result["error_code"], "EXTERNAL_PROVIDER_NOT_ALLOWLIST_ENABLED")

    def test_runtime_missing_is_not_eligible(self):
        with tempfile.TemporaryDirectory() as temp:
            result = diagnose("groq-2", entry(allowlisted=True), RuntimeModelState(Path(temp) / "runtime.json"), {"GROQ_TEST_KEY": "secret"})
        self.assertFalse(result["runtime_eligible"])
        self.assertEqual(result["eligibility_reason"], "EXTERNAL_MODEL_NOT_ELIGIBLE")

    def test_live_smoke_requires_confirmation_before_any_provider_call(self):
        with patch("codex_ai_router.groq_diagnostics.GroqProvider.complete") as complete:
            result = live_smoke("groq-2", entry(allowlisted=True), False, self.state_with_pass(), {"GROQ_TEST_KEY": "secret"})
        self.assertEqual(result["error_code"], "LIVE_CONFIRMATION_REQUIRED")
        complete.assert_not_called()

    def test_live_smoke_rejects_disabled_allowlist_without_provider_call(self):
        with patch("codex_ai_router.groq_diagnostics.GroqProvider.complete") as complete:
            result = live_smoke("groq-2", entry(), True, self.state_with_pass(), {"GROQ_TEST_KEY": "secret"})
        self.assertEqual(result["error_code"], "EXTERNAL_PROVIDER_NOT_ALLOWLIST_ENABLED")
        complete.assert_not_called()

    def test_diagnosis_does_not_include_secret_or_authorization(self):
        result = diagnose("groq-2", entry(), self.state_with_pass(), {"GROQ_TEST_KEY": "secret-value"})
        rendered = str(result)
        self.assertNotIn("secret-value", rendered)
        self.assertNotIn("Authorization", rendered)
        self.assertFalse(result["key_visible"])

    def test_groq_errors_are_not_collapsed_to_generic_503(self):
        self.assertEqual(_live_error(ProviderError("GROQ_MODEL_UNAVAILABLE")), "MODEL_NOT_FOUND")
        self.assertEqual(_live_error(ProviderError("GROQ_RATE_LIMIT")), "RATE_LIMITED")
        self.assertEqual(_live_error(ProviderError("GROQ_UPSTREAM_FORMAT_ERROR")), "UPSTREAM_FORMAT_ERROR")


if __name__ == "__main__":
    unittest.main()

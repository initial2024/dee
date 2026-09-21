from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "deepseek_router_capture_harness.py"
SPEC = importlib.util.spec_from_file_location("deepseek_router_capture_harness", SCRIPT)
assert SPEC and SPEC.loader
HARNESS = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = HARNESS
SPEC.loader.exec_module(HARNESS)


class DeepSeekRouterCaptureHarnessTests(unittest.TestCase):
    def test_fixture_mode_captures_allowlisted_router_summary_without_model_call(self) -> None:
        summary = HARNESS.run_fixture("FIXTURE_CAPTURE_MARKER")
        self.assertEqual(summary["mode"], "fixture")
        self.assertEqual(summary["request_sent"], "YES_LOCAL_FIXTURE_ONLY")
        self.assertEqual(summary["model_call_sent"], "NO")
        self.assertEqual(summary["deepseek_request_sent"], "NO")
        self.assertEqual((summary["http_status"], summary["exit_code"]), (200, 0))
        self.assertTrue(summary["stdout_captured"])
        self.assertTrue(summary["stderr_captured"])
        self.assertTrue(summary["response_body_captured"])
        self.assertTrue(summary["router_json_parsed"])
        self.assertTrue(summary["marker_found"])
        self.assertEqual(summary["provider_selected"], "deepseek-bridge-direct")
        self.assertFalse(summary["fallback_triggered"])
        self.assertTrue(summary["success_telemetry"])
        self.assertTrue(summary["capability_metadata_ok"])
        self.assertFalse(summary["temp_files_used"])
        self.assertTrue(summary["temp_files_cleaned"])

    def test_non_loopback_url_is_rejected_without_request(self) -> None:
        result = HARNESS.capture_json_post("https://example.invalid/deepseek-head/coordinate", {})
        self.assertEqual((result.exit_code, result.error_type, result.body), (2, "NON_LOOPBACK_URL_REJECTED", b""))

    def test_live_mode_requires_both_explicit_guards(self) -> None:
        with self.assertRaises(SystemExit) as missing_ack:
            HARNESS.main(["--live"])
        self.assertEqual(missing_ack.exception.code, 2)
        with self.assertRaises(SystemExit) as missing_task:
            HARNESS.main(["--live", "--i-understand-this-sends-one-model-request"])
        self.assertEqual(missing_task.exception.code, 2)


if __name__ == "__main__":
    unittest.main()

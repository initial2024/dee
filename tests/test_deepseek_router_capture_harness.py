from __future__ import annotations

import importlib.util
import json
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
        self.assertTrue(summary["request_marker_expected"])
        self.assertEqual(summary["response_marker_found"], "unknown")
        self.assertEqual(summary["marker_check_source"], "telemetry_only")
        self.assertFalse(summary["response_marker_required_for_pass"])
        self.assertEqual(summary["pass_criteria_mode"], "telemetry_provider_capture")
        self.assertEqual(summary["final_status"], "PASS")
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

    def test_marker_missing_is_optional_only_in_telemetry_provider_capture_mode(self) -> None:
        body = {
            "status": "PASS", "selected_brain": "deepseek-bridge-direct", "fallback_triggered": False,
            "bridge_send_attempted": "YES", "router_capability_metadata_sent": True,
            "response_contains_marker": False, "prompt_marker_expected": None,
        }
        summary = HARNESS._allowlisted_summary(
            HARNESS.CaptureResult(200, 0, json.dumps(body).encode("utf-8")),
            mode="fixture", marker="L4_R8J_OPTIONAL_MARKER", stdout="", stderr="",
        )
        self.assertEqual(summary["response_marker_found"], "unknown")
        self.assertEqual(summary["marker_check_source"], "telemetry_only")
        self.assertFalse(summary["response_marker_required_for_pass"])
        self.assertEqual(summary["final_status"], "PASS")

        required = HARNESS.run_fixture(
            "L4_R8J_REQUIRED_MARKER",
            response_marker_required_for_pass=True,
        )
        self.assertTrue(required["response_marker_required_for_pass"])
        self.assertEqual(required["response_marker_found"], "unknown")
        self.assertEqual(required["final_status"], "FAIL_TELEMETRY_PROVIDER_CAPTURE")

    def test_assistant_text_marker_status_is_yes_or_no_without_exposure(self) -> None:
        base = {
            "status": "PASS", "selected_brain": "deepseek-bridge-direct", "fallback_triggered": False,
            "bridge_send_attempted": "YES", "router_capability_metadata_sent": True,
        }
        expected_marker = "L4_R8J_ASSISTANT_TEXT_MARKER"
        found = HARNESS._allowlisted_summary(
            HARNESS.CaptureResult(200, 0, json.dumps({**base, "assistant_text": f"model-only: {expected_marker}"}).encode("utf-8")),
            mode="fixture", marker=expected_marker, stdout="", stderr="",
        )
        missing = HARNESS._allowlisted_summary(
            HARNESS.CaptureResult(200, 0, json.dumps({**base, "assistant_text": "different text"}).encode("utf-8")),
            mode="fixture", marker=expected_marker, stdout="", stderr="",
        )
        self.assertEqual((found["response_marker_found"], found["marker_check_source"]), ("yes", "assistant_text"))
        self.assertEqual((missing["response_marker_found"], missing["marker_check_source"]), ("no", "assistant_text"))
        self.assertNotIn("model-only", json.dumps(found))

    def test_provider_fallback_or_telemetry_failure_cannot_pass(self) -> None:
        base = {
            "status": "PASS", "selected_brain": "deepseek-bridge-direct", "fallback_triggered": False,
            "bridge_send_attempted": "YES", "router_capability_metadata_sent": True,
        }
        for changed in ({"selected_brain": "unknown"}, {"fallback_triggered": True}, {"bridge_send_attempted": "NO"}, {"router_capability_metadata_sent": False}):
            body = {**base, **changed}
            summary = HARNESS._allowlisted_summary(
                HARNESS.CaptureResult(200, 0, json.dumps(body).encode("utf-8")),
                mode="fixture", marker="L4_R8J_GUARD", stdout="", stderr="",
            )
            self.assertEqual(summary["final_status"], "FAIL_TELEMETRY_PROVIDER_CAPTURE")

    def test_summary_does_not_expose_unallowlisted_response_fields(self) -> None:
        body = {
            "status": "PASS", "selected_brain": "deepseek-bridge-direct", "fallback_triggered": False,
            "bridge_send_attempted": "YES", "router_capability_metadata_sent": True,
            "authorization": "fixture-secret", "cookie": "fixture-cookie", "assistant_text": "fixture-secret",
        }
        summary = HARNESS._allowlisted_summary(
            HARNESS.CaptureResult(200, 0, json.dumps(body).encode("utf-8")),
            mode="fixture", marker="L4_R8J_REDACTION", stdout="", stderr="",
        )
        encoded = json.dumps(summary)
        self.assertNotIn("fixture-secret", encoded)
        self.assertNotIn("fixture-cookie", encoded)


if __name__ == "__main__":
    unittest.main()

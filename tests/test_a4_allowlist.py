from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_ai_router.provider_allowlist import model_catalog
from codex_ai_router.server import RouterService


class FakeUpstream:
    status = 200

    def __init__(self, payload: dict):
        self.payload = payload

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class A4AllowlistTests(unittest.TestCase):
    def test_disabled_external_provider_is_not_catalogued(self):
        records = [{"id": "external", "type": "EXTERNAL_API_ALLOWED", "endpoint": "https://example.invalid/v1", "models": ["external-fast"], "enabled": False, "status": "DISABLED"}]
        with patch("codex_ai_router.provider_allowlist.providers", return_value=records):
            self.assertNotIn("external-fast", {item["id"] for item in model_catalog()})

    def test_manual_plan_is_structured_without_backend_call(self):
        provider = {"id": "manual-plan", "type": "MANUAL_PLAN", "endpoint": None, "models": ["manual-plan"], "enabled": True, "status": "ENABLED"}
        service = RouterService()
        with patch("codex_ai_router.server.allowlisted_providers", return_value=[provider]), patch("codex_ai_router.server.resolve_allowlisted", return_value=(provider, None)):
            result = service.respond({"model": "manual-plan", "input": "inspect the diff"})
        text = result["output_text"]
        self.assertIn("Problem Summary:", text)
        self.assertIn("Requires User Confirmation: YES", text)

    def test_tools_are_rejected_instead_of_faked(self):
        service = RouterService()
        with self.assertRaisesRegex(RuntimeError, "TOOLS_NOT_SUPPORTED_BY_BACKEND"):
            service.respond({"model": "deepseek-web", "input": "hello", "tools": [{"type": "function"}]})

    def test_allowlisted_chat_is_normalized_and_ledger_has_metadata_only(self):
        provider = {"id": "bridge", "type": "DEEPSEEK_WEB_BRIDGE", "endpoint": "http://127.0.0.1:8792/v1", "models": ["deepseek-web"], "enabled": True, "status": "ENABLED"}
        with tempfile.TemporaryDirectory() as temp:
            ledger = Path(temp) / "calls.jsonl"
            with patch.dict(os.environ, {"XIAOYU_ROUTER_CALL_LEDGER": str(ledger)}, clear=False), patch("codex_ai_router.server.allowlisted_providers", return_value=[provider]), patch("codex_ai_router.server.resolve_allowlisted", return_value=(provider, None)), patch("codex_ai_router.server.urlopen", return_value=FakeUpstream({"choices": [{"message": {"content": "visible"}}]})):
                result = RouterService().chat_completion({"model": "deepseek-web", "messages": [{"role": "user", "content": "hello"}]})
            self.assertEqual(result["choices"][0]["message"]["content"], "visible")
            raw = ledger.read_text(encoding="utf-8")
            self.assertIn('"saved_body":false', raw)
            self.assertNotIn("hello", raw)
            self.assertNotIn("visible", raw)

    def test_missing_external_key_is_auth_missing(self):
        provider = {"id": "external", "type": "EXTERNAL_API_ALLOWED", "endpoint": "https://example.invalid/v1", "models": ["external-fast"], "enabled": True, "status": "AUTH_MISSING"}
        with patch("codex_ai_router.server.allowlisted_providers", return_value=[provider]), patch("codex_ai_router.server.resolve_allowlisted", return_value=(provider, "AUTH_MISSING")):
            with self.assertRaisesRegex(RuntimeError, "AUTH_MISSING"):
                RouterService().respond({"model": "external-fast", "input": "hello"})


if __name__ == "__main__":
    unittest.main()

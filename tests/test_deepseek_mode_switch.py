from __future__ import annotations

import io
import json
import unittest
from urllib.error import HTTPError

from codex_ai_router.deepseek_mode_switch import ModeSwitchProxyError, proxy_mode_switch, validate_mode_switch_payload


class Response:
    status = 200
    def __init__(self, body): self.body = body
    def read(self): return json.dumps(self.body).encode("utf-8")
    def __enter__(self): return self
    def __exit__(self, *_): return False


def valid_body():
    return {"matched": True, "promptSent": False, "clickSend": False, "uploadAttempted": False,
            "before": {"current_base_mode": "quick", "current_thinking": False, "current_search": True, "current_modality": "text"},
            "after": {"current_base_mode": "quick", "current_thinking": False, "current_search": False, "current_modality": "text"}}


class ModeSwitchProxyTests(unittest.TestCase):
    def test_payload_rejects_prompts_and_attachments(self):
        for key in ("prompt", "messages", "input", "text", "file_path", "image_path", "attachment_id", "cookie", "token", "authorization", "storageState"):
            with self.assertRaises(ModeSwitchProxyError):
                validate_mode_switch_payload({"target_mode": "quick_plain", "verify": True, key: "x"})

    def test_proxy_sends_only_allowlisted_body_and_compacts_response(self):
        def opener(request, timeout):
            self.assertEqual(request.full_url, "http://127.0.0.1:8791/mode-switch")
            self.assertEqual(json.loads(request.data.decode("utf-8")), {"target_mode": "quick_plain", "verify": True})
            return Response(valid_body())
        status, body = proxy_mode_switch({"target_mode": "quick_plain", "verify": True}, opener=opener)
        self.assertEqual(status, 200); self.assertTrue(body["matched"]); self.assertFalse(body["prompt_sent"])

    def test_proxy_keeps_explicit_upstream_error_code(self):
        def opener(_request, timeout):
            raise HTTPError("http://127.0.0.1:8791/mode-switch", 400, "bad", {}, io.BytesIO(b'{"error":{"code":"DEEPSEEK_FILE_UPLOAD_UNAVAILABLE"}}'))
        status, body = proxy_mode_switch({"target_mode": "file_extract", "verify": True}, opener=opener)
        self.assertEqual(status, 400); self.assertEqual(body["error_code"], "DEEPSEEK_FILE_UPLOAD_UNAVAILABLE")

    def test_proxy_rejects_mismatch(self):
        bad = valid_body(); bad["matched"] = False
        status, body = proxy_mode_switch({"target_mode": "quick_plain", "verify": True}, opener=lambda *_, **__: Response(bad))
        self.assertEqual(status, 502); self.assertEqual(body["error_code"], "MODE_SWITCH_VERIFY_FAILED")


if __name__ == "__main__": unittest.main()

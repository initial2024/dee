"""Strict localhost proxy for Bridge mode switching.

This module carries only mode metadata.  It never accepts prompt, attachment,
or credential fields and never persists either request or upstream body.
"""
from __future__ import annotations

import json
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


BRIDGE_MODE_SWITCH_URL = "http://127.0.0.1:8791/mode-switch"
ALLOWED_MODES = {
    "quick_plain", "quick_thinking", "expert_plain", "expert_thinking",
    "quick_search", "expert_thinking_search", "vision_expert_thinking", "file_extract",
}
ALLOWED_FIELDS = {"target_mode", "verify"}


class ModeSwitchProxyError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def validate_mode_switch_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if set(payload) - ALLOWED_FIELDS:
        raise ModeSwitchProxyError("MODE_SWITCH_PAYLOAD_REJECTED")
    target = payload.get("target_mode")
    if target not in ALLOWED_MODES or payload.get("verify") is not True:
        raise ModeSwitchProxyError("MODE_SWITCH_PAYLOAD_REJECTED")
    return {"target_mode": target, "verify": True}


def _snapshot(value: object) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    return {key: source.get(key) for key in ("current_base_mode", "current_thinking", "current_search", "current_modality")}


def proxy_mode_switch(payload: dict[str, Any], *, opener: Callable[..., Any] = urlopen) -> tuple[int, dict[str, Any]]:
    safe_payload = validate_mode_switch_payload(payload)
    request = Request(
        BRIDGE_MODE_SWITCH_URL,
        data=json.dumps(safe_payload).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with opener(request, timeout=10) as response:
            body = json.loads(response.read().decode("utf-8"))
            status = int(getattr(response, "status", 200))
    except HTTPError as error:
        code = "MODE_SWITCH_PROXY_FAILED"
        try:
            parsed = json.loads(error.read().decode("utf-8"))
            code = str((parsed.get("error") or {}).get("code") or code)
        except Exception:
            pass
        return int(error.code), {"status": "ERROR", "error_code": code}
    except (URLError, OSError, ValueError, json.JSONDecodeError):
        return 503, {"status": "ERROR", "error_code": "DEEPSEEK_BRIDGE_OFFLINE"}
    before, after = _snapshot(body.get("before")), _snapshot(body.get("after"))
    matched = body.get("matched") is True
    if status != 200 or not matched or body.get("promptSent") is not False or body.get("clickSend") is not False or body.get("uploadAttempted") is not False:
        return 502, {"status": "ERROR", "error_code": "MODE_SWITCH_VERIFY_FAILED", "before": before, "after": after, "matched": False}
    return 200, {"status": "PASS", "target_mode": safe_payload["target_mode"], "before": before, "after": after, "matched": True, "prompt_sent": False, "click_send": False, "upload_attempted": False}

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
    "quick_search", "expert_thinking_search", "expert_max_review", "vision_expert_thinking", "file_extract",
}
ALLOWED_FIELDS = {"target_mode", "target_profile", "ui_generation", "reasoning_strength", "search", "vision", "file", "verify"}
FORBIDDEN_FIELDS = {"prompt", "messages", "input", "text", "file_path", "image_path", "attachment_id", "cookie", "token", "authorization", "storageState"}


class ModeSwitchProxyError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def validate_mode_switch_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) & FORBIDDEN_FIELDS or set(payload) - ALLOWED_FIELDS:
        raise ModeSwitchProxyError("MODE_SWITCH_PAYLOAD_REJECTED")
    target = payload.get("target_mode") or payload.get("target_profile")
    direct_fields = {key for key in ("ui_generation", "reasoning_strength", "search", "vision", "file") if key in payload}
    if target is None and not direct_fields:
        raise ModeSwitchProxyError("MODE_SWITCH_PAYLOAD_REJECTED")
    if target is not None and target not in ALLOWED_MODES:
        raise ModeSwitchProxyError("MODE_SWITCH_PAYLOAD_REJECTED")
    if payload.get("verify") is not True:
        raise ModeSwitchProxyError("MODE_SWITCH_PAYLOAD_REJECTED")
    safe: dict[str, Any] = {"verify": True}
    if target is not None:
        safe["target_profile" if "target_profile" in payload and "target_mode" not in payload else "target_mode"] = target
    if "ui_generation" in payload:
        if payload["ui_generation"] not in {"legacy", "three_in_one", "unknown"}:
            raise ModeSwitchProxyError("MODE_SWITCH_PAYLOAD_REJECTED")
        safe["ui_generation"] = payload["ui_generation"]
    if "reasoning_strength" in payload:
        if payload["reasoning_strength"] not in {"off", "low", "medium", "high", "max"}:
            raise ModeSwitchProxyError("MODE_SWITCH_PAYLOAD_REJECTED")
        safe["reasoning_strength"] = payload["reasoning_strength"]
    for key in ("search", "vision", "file"):
        if key in payload:
            if not isinstance(payload[key], bool):
                raise ModeSwitchProxyError("MODE_SWITCH_PAYLOAD_REJECTED")
            safe[key] = payload[key]
    return safe


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
    return 200, {"status": "PASS", "target_mode": safe_payload.get("target_mode"), "target_profile": safe_payload.get("target_profile"), "reasoning_strength": safe_payload.get("reasoning_strength"), "search": safe_payload.get("search"), "vision": safe_payload.get("vision", False), "file": safe_payload.get("file", False), "before": before, "after": after, "matched": True, "prompt_sent": False, "click_send": False, "upload_attempted": False}

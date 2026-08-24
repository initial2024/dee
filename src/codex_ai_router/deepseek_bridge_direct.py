"""Direct, loopback-only DeepSeek Web Bridge brain adapter.

This adapter talks only to the already-running browser bridge on
``127.0.0.1:8791``.  It deliberately does not use the Worker/Wrangler path,
does not carry credentials, and sends a text-only request without tool fields.
The returned text is advisory input for Local Agent plan generation; it never
performs file, shell, Git, or deployment actions.
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .deepseek_head import advisory_result
from .deepseek_modes import MODE_ALIASES, mode_alias, select_deepseek_mode
from .response_compat import ResponseCompatibilityError, normalize_chat_completion


BRIDGE_DIRECT_BASE = "http://127.0.0.1:8791/v1"
BRIDGE_DIRECT_HEALTH = "http://127.0.0.1:8791/health"
BRIDGE_DIRECT_MODE_PROBE = "http://127.0.0.1:8791/mode-probe"
BRIDGE_DIRECT_MODEL = "deepseek-web"
TEXT_ONLY_SYSTEM = (
    "当前为 TEXT_ONLY 计划模式。你不能调用工具，也不能声称已经读取、修改、运行、提交或部署。"
    "只能基于提供的上下文输出分析、Agent Plan、补丁建议、手动命令建议、风险检查和下一步建议。"
    "如果需要真实文件操作，必须交给 Codex 官方工具并等待人工确认。"
)


def _strict_loopback(url: str, *, path: str | None = None) -> bool:
    """Accept only plain HTTP to the fixed local bridge socket."""
    try:
        parsed = urlsplit(str(url))
    except ValueError:
        return False
    try:
        port = parsed.port
    except ValueError:
        return False
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or port != 8791:
        return False
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        return False
    if path is not None and parsed.path != path:
        return False
    return True


def _safe_error_code(value: object) -> str:
    code = str(value or "").strip().upper()
    return code if code and len(code) <= 80 and all(ch.isalnum() or ch in "_.:-" for ch in code) else ""


def _read_json(request: Request | str, timeout: int, opener: Callable[..., Any]) -> tuple[int, dict[str, Any]]:
    with opener(request, timeout=timeout) as response:
        raw = response.read().decode("utf-8")
        parsed = json.loads(raw) if raw else {}
        return int(getattr(response, "status", 200)), parsed if isinstance(parsed, dict) else {}


def _error_from_body(body: bytes) -> str:
    try:
        parsed = json.loads(body.decode("utf-8")) if body else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ""
    if not isinstance(parsed, dict):
        return ""
    error = parsed.get("error")
    if isinstance(error, dict):
        return _safe_error_code(error.get("code"))
    return _safe_error_code(parsed.get("code") or parsed.get("error_code"))


_TEXT_MODE_PROFILES = {
    "quick_plain": ("quick", False, False, "text"),
    "quick_thinking": ("quick", True, False, "text"),
    "quick_search": ("quick", False, True, "text"),
    "expert_plain": ("expert", False, False, "text"),
    "expert_thinking": ("expert", True, False, "text"),
    "expert_thinking_search": ("expert", True, True, "text"),
}


def _bridge_error(
    code: str,
    request_id: str,
    started: float,
    *,
    stage: str = "before_bridge_send",
    bridge_send_attempted: bool = False,
    ui_send_attempt_count: int = 0,
) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "status": code,
        "error_code": code,
        "duration_ms": round((time.monotonic() - started) * 1000),
        "endpoint_mode": "DEEPSEEK_BRIDGE_DIRECT",
        "loopback_only": "YES",
        "provider_error_stage": stage,
        "bridge_send_attempted": "YES" if bridge_send_attempted else "NO",
        "bridge_ui_send_attempt_count": max(0, int(ui_send_attempt_count)),
        "model_output_available": "NO",
        "prompt_response_logged": "NO",
        "secrets_logged": "NO",
    }


def _ui_send_attempt_count(health: dict[str, Any]) -> int:
    bridge = health.get("bridge") if isinstance(health.get("bridge"), dict) else health
    value = bridge.get("uiSendAttemptCount", bridge.get("ui_send_attempt_count", 0)) if isinstance(bridge, dict) else 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _requested_text_mode(selected_mode: str | None, search: bool | None) -> tuple[str | None, str | None]:
    if selected_mode is None:
        return None, None
    mode = str(selected_mode).strip().lower()
    if mode not in _TEXT_MODE_PROFILES or mode not in MODE_ALIASES:
        return None, "DEEPSEEK_BRIDGE_DIRECT_MODE_PARAM_INVALID"
    if search is False and "search" in mode:
        return None, "DEEPSEEK_BRIDGE_DIRECT_MODE_PARAM_INVALID"
    return mode, None


def _probe_matches_requested_mode(probe: dict[str, Any], mode: str) -> bool:
    base, thinking, search, modality = _TEXT_MODE_PROFILES[mode]
    return (
        probe.get("current_base_mode") == base
        and probe.get("current_thinking") is thinking
        and probe.get("current_search") is search
        and probe.get("current_modality") == modality
    )


def _health_error(health: dict[str, Any]) -> str | None:
    bridge = health.get("bridge") if isinstance(health.get("bridge"), dict) else health
    deepseek = health.get("deepseek") if isinstance(health.get("deepseek"), dict) else {}
    if bridge.get("busy") is True:
        return "DEEPSEEK_BRIDGE_BUSY"
    if deepseek.get("loginRequired") is True or deepseek.get("loggedIn") is False:
        return "DEEPSEEK_LOGIN_REQUIRED"
    if deepseek and (deepseek.get("loggedIn") is not True):
        return "DEEPSEEK_LOGIN_REQUIRED"
    if deepseek and (deepseek.get("pageReady") is not True or deepseek.get("inputReady") is not True):
        return "DEEPSEEK_MODE_UNAVAILABLE"
    if health.get("ok") is False and not deepseek:
        return "DEEPSEEK_MODE_UNAVAILABLE"
    return None


def run_deepseek_bridge_direct(
    task: str,
    task_type: str = "AUTO",
    *,
    api_base: str = BRIDGE_DIRECT_BASE,
    health_url: str = BRIDGE_DIRECT_HEALTH,
    mode_probe_url: str = BRIDGE_DIRECT_MODE_PROBE,
    selected_mode: str | None = None,
    search: bool | None = None,
    timeout: int = 90,
    opener: Callable[..., Any] = urlopen,
) -> dict[str, Any]:
    """Make one explicit non-streaming TEXT_ONLY request to the local bridge."""
    request_id = "deepseek_direct_" + uuid.uuid4().hex
    started = time.monotonic()
    base = api_base.rstrip("/")
    completion_url = base + "/chat/completions"
    if not str(task).strip():
        return _bridge_error("DEEPSEEK_BRIDGE_DIRECT_PAYLOAD_INVALID", request_id, started)
    if not _strict_loopback(health_url, path="/health") or not _strict_loopback(mode_probe_url, path="/mode-probe") or not _strict_loopback(completion_url, path="/v1/chat/completions"):
        return _bridge_error("DEEPSEEK_BRIDGE_DIRECT_URL_NOT_CONFIGURED", request_id, started)
    requested_mode, mode_error = _requested_text_mode(selected_mode, search)
    if mode_error:
        return _bridge_error(mode_error, request_id, started)
    try:
        _status, health = _read_json(health_url, min(timeout, 5), opener)
    except HTTPError as exc:
        try:
            code = _error_from_body(exc.read())
        except OSError:
            code = ""
        mapped = {
            "LOGIN_REQUIRED": "DEEPSEEK_LOGIN_REQUIRED",
            "BRIDGE_BUSY": "DEEPSEEK_BRIDGE_BUSY",
            "MODE_NOT_AVAILABLE": "DEEPSEEK_MODE_UNAVAILABLE",
            "MODE_SWITCH_FAILED": "DEEPSEEK_MODE_UNAVAILABLE",
            "UI_CHANGED": "DEEPSEEK_MODE_UNAVAILABLE",
            "INPUT_NOT_FOUND": "DEEPSEEK_MODE_UNAVAILABLE",
            "BROWSER_NOT_CONNECTED": "DEEPSEEK_BRIDGE_OFFLINE",
        }.get(code)
        return _bridge_error(mapped or ("DEEPSEEK_LOGIN_REQUIRED" if exc.code == 401 else "DEEPSEEK_BRIDGE_OFFLINE"), request_id, started)
    except (URLError, TimeoutError, OSError, json.JSONDecodeError):
        return _bridge_error("DEEPSEEK_BRIDGE_OFFLINE", request_id, started)
    ui_send_attempt_count = _ui_send_attempt_count(health)
    health_error = _health_error(health)
    if health_error:
        return _bridge_error(health_error, request_id, started)
    try:
        probe_status, probe = _read_json(mode_probe_url, min(timeout, 5), opener)
    except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError):
        return _bridge_error("DEEPSEEK_MODE_UNAVAILABLE", request_id, started)
    if probe_status != 200 or probe.get("ui_changed") is True:
        return _bridge_error("DEEPSEEK_MODE_UNAVAILABLE", request_id, started)
    if requested_mode is not None and not _probe_matches_requested_mode(probe, requested_mode):
        return _bridge_error("DEEPSEEK_BRIDGE_DIRECT_MODE_PARAM_INVALID", request_id, started, ui_send_attempt_count=ui_send_attempt_count)
    try:
        selected = select_deepseek_mode(
            str(task),
            explicit_model_alias=mode_alias(requested_mode) if requested_mode else None,
            availability=probe.get("modes") if isinstance(probe.get("modes"), dict) else {},
            search_allowed=search is not False,
        )
    except (KeyError, TypeError, ValueError):
        return _bridge_error("DEEPSEEK_BRIDGE_DIRECT_CONTEXT_BUILD_FAILED", request_id, started, ui_send_attempt_count=ui_send_attempt_count)
    if not selected["mode_available"]:
        return _bridge_error(str(selected.get("fallback_reason") or "DEEPSEEK_MODE_UNAVAILABLE"), request_id, started, ui_send_attempt_count=ui_send_attempt_count)
    selected_model = str(selected["selected_model_alias"])
    payload = {
        "model": selected_model,
        "messages": [{"role": "system", "content": TEXT_ONLY_SYSTEM}, {"role": "user", "content": str(task).strip()}],
        "stream": False,
    }
    request = Request(completion_url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers={"Content-Type": "application/json", "Accept": "application/json"}, method="POST")
    try:
        _status, upstream = _read_json(request, timeout, opener)
        normalized = normalize_chat_completion(upstream, model=selected_model)
        text = normalized["choices"][0]["message"]["content"]
        result = advisory_result(text, task_type)
        return {"request_id": request_id, "status": "PASS", "model": selected_model, "selected_mode": selected["selected_mode"], "task_difficulty": selected["task_difficulty"], "performance_mode": selected["performance_mode"], "stream_mode": "non_stream", "endpoint_mode": "DEEPSEEK_BRIDGE_DIRECT", "loopback_only": "YES", "provider_error_stage": None, "bridge_send_attempted": "YES", "bridge_ui_send_attempt_count": ui_send_attempt_count + 1, "model_output_available": "YES", "duration_ms": round((time.monotonic() - started) * 1000), "prompt_response_logged": "NO", "secrets_logged": "NO", **result}
    except HTTPError as exc:
        try:
            code = _error_from_body(exc.read())
        except OSError:
            code = ""
        mapped = {"LOGIN_REQUIRED": "DEEPSEEK_LOGIN_REQUIRED", "BRIDGE_BUSY": "DEEPSEEK_BRIDGE_BUSY", "MODE_NOT_AVAILABLE": "DEEPSEEK_MODE_UNAVAILABLE", "MODE_SWITCH_FAILED": "DEEPSEEK_MODE_UNAVAILABLE", "UI_CHANGED": "DEEPSEEK_MODE_UNAVAILABLE", "INPUT_NOT_FOUND": "DEEPSEEK_MODE_UNAVAILABLE", "BROWSER_NOT_CONNECTED": "DEEPSEEK_BRIDGE_OFFLINE"}.get(code)
        if mapped:
            return _bridge_error(mapped, request_id, started, stage="bridge_send", bridge_send_attempted=True, ui_send_attempt_count=ui_send_attempt_count + 1)
        if exc.code == 429:
            return _bridge_error("DEEPSEEK_BRIDGE_BUSY", request_id, started, stage="bridge_send", bridge_send_attempted=True, ui_send_attempt_count=ui_send_attempt_count + 1)
        if exc.code in {401, 503}:
            return _bridge_error("DEEPSEEK_LOGIN_REQUIRED" if exc.code == 401 else "DEEPSEEK_BRIDGE_OFFLINE", request_id, started, stage="bridge_send", bridge_send_attempted=True, ui_send_attempt_count=ui_send_attempt_count + 1)
        return _bridge_error("DEEPSEEK_MODE_UNAVAILABLE", request_id, started, stage="bridge_send", bridge_send_attempted=True, ui_send_attempt_count=ui_send_attempt_count + 1)
    except ResponseCompatibilityError:
        return _bridge_error("DEEPSEEK_EMPTY_RESPONSE", request_id, started, stage="bridge_send", bridge_send_attempted=True, ui_send_attempt_count=ui_send_attempt_count + 1)
    except (URLError, TimeoutError, OSError, json.JSONDecodeError):
        return _bridge_error("DEEPSEEK_BRIDGE_OFFLINE", request_id, started, stage="bridge_send", bridge_send_attempted=True, ui_send_attempt_count=ui_send_attempt_count + 1)


__all__ = ["BRIDGE_DIRECT_BASE", "BRIDGE_DIRECT_HEALTH", "run_deepseek_bridge_direct"]

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
from .response_compat import ResponseCompatibilityError, normalize_chat_completion


BRIDGE_DIRECT_BASE = "http://127.0.0.1:8791/v1"
BRIDGE_DIRECT_HEALTH = "http://127.0.0.1:8791/health"
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


def _bridge_error(code: str, request_id: str, started: float) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "status": code,
        "error_code": code,
        "duration_ms": round((time.monotonic() - started) * 1000),
        "endpoint_mode": "DEEPSEEK_BRIDGE_DIRECT",
        "loopback_only": "YES",
        "prompt_response_logged": "NO",
        "secrets_logged": "NO",
    }


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
    timeout: int = 90,
    opener: Callable[..., Any] = urlopen,
) -> dict[str, Any]:
    """Make one explicit non-streaming TEXT_ONLY request to the local bridge."""
    request_id = "deepseek_direct_" + uuid.uuid4().hex
    started = time.monotonic()
    base = api_base.rstrip("/")
    completion_url = base + "/chat/completions"
    if not str(task).strip() or not _strict_loopback(health_url, path="/health") or not _strict_loopback(completion_url, path="/v1/chat/completions"):
        return _bridge_error("DEEPSEEK_MODE_UNAVAILABLE", request_id, started)
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
    health_error = _health_error(health)
    if health_error:
        return _bridge_error(health_error, request_id, started)
    payload = {
        "model": BRIDGE_DIRECT_MODEL,
        "messages": [{"role": "system", "content": TEXT_ONLY_SYSTEM}, {"role": "user", "content": str(task).strip()}],
        "stream": False,
    }
    request = Request(completion_url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers={"Content-Type": "application/json", "Accept": "application/json"}, method="POST")
    try:
        _status, upstream = _read_json(request, timeout, opener)
        normalized = normalize_chat_completion(upstream, model=BRIDGE_DIRECT_MODEL)
        text = normalized["choices"][0]["message"]["content"]
        result = advisory_result(text, task_type)
        return {"request_id": request_id, "status": "PASS", "model": BRIDGE_DIRECT_MODEL, "stream_mode": "non_stream", "endpoint_mode": "DEEPSEEK_BRIDGE_DIRECT", "loopback_only": "YES", "duration_ms": round((time.monotonic() - started) * 1000), "prompt_response_logged": "NO", "secrets_logged": "NO", **result}
    except HTTPError as exc:
        try:
            code = _error_from_body(exc.read())
        except OSError:
            code = ""
        mapped = {"LOGIN_REQUIRED": "DEEPSEEK_LOGIN_REQUIRED", "BRIDGE_BUSY": "DEEPSEEK_BRIDGE_BUSY", "MODE_NOT_AVAILABLE": "DEEPSEEK_MODE_UNAVAILABLE", "MODE_SWITCH_FAILED": "DEEPSEEK_MODE_UNAVAILABLE", "UI_CHANGED": "DEEPSEEK_MODE_UNAVAILABLE", "INPUT_NOT_FOUND": "DEEPSEEK_MODE_UNAVAILABLE", "BROWSER_NOT_CONNECTED": "DEEPSEEK_BRIDGE_OFFLINE"}.get(code)
        if mapped:
            return _bridge_error(mapped, request_id, started)
        if exc.code == 429:
            return _bridge_error("DEEPSEEK_BRIDGE_BUSY", request_id, started)
        if exc.code in {401, 503}:
            return _bridge_error("DEEPSEEK_LOGIN_REQUIRED" if exc.code == 401 else "DEEPSEEK_BRIDGE_OFFLINE", request_id, started)
        return _bridge_error("DEEPSEEK_MODE_UNAVAILABLE", request_id, started)
    except ResponseCompatibilityError:
        return _bridge_error("DEEPSEEK_EMPTY_RESPONSE", request_id, started)
    except (URLError, TimeoutError, OSError, json.JSONDecodeError):
        return _bridge_error("DEEPSEEK_BRIDGE_OFFLINE", request_id, started)


__all__ = ["BRIDGE_DIRECT_BASE", "BRIDGE_DIRECT_HEALTH", "run_deepseek_bridge_direct"]

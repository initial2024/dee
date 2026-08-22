"""Safe DeepSeek Web mode probing and task-to-mode selection.

The probe is deliberately read-only: it asks the local Browser Bridge for
visible-control metadata and never sends a prompt or clicks the send control.
No credential, cookie, or browser storage value is handled here.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEEPSEEK_HEALTH = "http://127.0.0.1:8791/health"
DEEPSEEK_MODE_PROBE = "http://127.0.0.1:8791/mode-probe"
MODE_ALIASES = {
    "normal": "deepseek-web",
    "search": "deepseek-web-search",
    "thinking": "deepseek-web-thinking",
    "expert": "deepseek-web-expert",
    "auto": "deepseek-web-auto",
}
MODE_ORDER = ("normal", "search", "thinking", "expert")
MODE_STATUSES = {"AVAILABLE", "UNAVAILABLE", "UI_PROBE_FAILED", "LOGIN_REQUIRED", "CAPTCHA_REQUIRED", "ACCOUNT_RISK", "RATE_LIMITED", "UNKNOWN"}
SEARCH_TERMS = ("最新", "今天", "当前", "搜索", "查一下", "网页", "官网", "价格", "最近", "文档最新版本", "新闻", "发布日期")
THINKING_TERMS = ("严谨分析", "架构", "审计", "复杂", "多阶段", "方案", "风险", "debug", "设计", "长日志")
EXPERT_TERMS = ("代码", "codex", "worker", "api", "provider", "测试", "报错", "兼容", "部署", "性能")


def mode_alias(mode: str) -> str:
    return MODE_ALIASES.get(mode, MODE_ALIASES["normal"])


def _state_path() -> Path:
    return Path(os.getenv("XIAOYU_ROUTER_DEEPSEEK_MODE_STATE", str(Path.home() / ".codex-ai-router" / "deepseek-mode-probe.json")))


def _write_state(payload: dict[str, Any], path: Path | None = None) -> None:
    target = path or _state_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError:
        # Probe success must never turn into a request failure because a local
        # metadata file is read-only or unavailable.
        return


def load_probe_state(path: Path | None = None) -> dict[str, Any]:
    target = path or _state_path()
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _classify_error(status: int, body: dict[str, Any]) -> str:
    code = str((body.get("error") or {}).get("code", "")) if isinstance(body.get("error"), dict) else ""
    deepseek = body.get("deepseek") if isinstance(body.get("deepseek"), dict) else {}
    if code in {"LOGIN_REQUIRED"} or deepseek.get("loginRequired") is True:
        return "LOGIN_REQUIRED"
    if code in {"CAPTCHA_PRESENT", "RISK_CONTROL_PRESENT"}:
        return "CAPTCHA_REQUIRED" if code == "CAPTCHA_PRESENT" else "ACCOUNT_RISK"
    if status == 429:
        return "RATE_LIMITED"
    return "UNKNOWN"


def _probe_request(url: str, timeout: float = 3.0) -> tuple[int, dict[str, Any]]:
    if not url.startswith("http://127.0.0.1:"):
        raise ValueError("NON_LOOPBACK_PROBE_URL")
    with urlopen(Request(url, method="GET"), timeout=timeout) as response:
        raw = json.loads(response.read().decode("utf-8"))
        return int(getattr(response, "status", 200)), raw if isinstance(raw, dict) else {}


def _unknown_modes(status: str = "UNKNOWN") -> dict[str, dict[str, Any]]:
    return {mode: {"status": status, "controlDetected": False, "controllable": False} for mode in MODE_ORDER}


def probe_deepseek_modes(*, health_url: str = DEEPSEEK_HEALTH, probe_url: str = DEEPSEEK_MODE_PROBE, timeout: float = 3.0, state_path: Path | None = None) -> dict[str, Any]:
    """Probe visible mode controls without prompt submission or UI clicks."""
    result: dict[str, Any] = {"probe": "READ_ONLY", "promptSent": False, "clickSend": False, "modes": _unknown_modes()}
    try:
        health_status, health = _probe_request(health_url, timeout)
    except HTTPError as exc:
        body: dict[str, Any] = {}
        try:
            body = json.loads(exc.read().decode("utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
        status = _classify_error(exc.code, body)
        result.update({"status": status, "error_code": status, "modes": _unknown_modes(status)})
        _write_state(result, state_path)
        return result
    except (URLError, TimeoutError, OSError, json.JSONDecodeError, ValueError):
        result.update({"status": "UNAVAILABLE", "error_code": "BRIDGE_OFFLINE", "modes": _unknown_modes("UNKNOWN")})
        _write_state(result, state_path)
        return result
    if health_status != 200:
        status = _classify_error(health_status, health)
        result.update({"status": status, "error_code": status, "modes": _unknown_modes(status)})
        _write_state(result, state_path)
        return result
    try:
        probe_status, probe = _probe_request(probe_url, timeout)
    except HTTPError as exc:
        result.update({"status": "UI_PROBE_FAILED", "error_code": "UI_PROBE_ENDPOINT_UNAVAILABLE", "modes": _unknown_modes("UI_PROBE_FAILED")})
        _write_state(result, state_path)
        return result
    except (URLError, TimeoutError, OSError, json.JSONDecodeError, ValueError):
        result.update({"status": "UI_PROBE_FAILED", "error_code": "UI_PROBE_ENDPOINT_UNAVAILABLE", "modes": _unknown_modes("UI_PROBE_FAILED")})
        _write_state(result, state_path)
        return result
    raw_modes = probe.get("modes") if isinstance(probe.get("modes"), dict) else {}
    modes = _unknown_modes("UI_PROBE_FAILED")
    for mode in MODE_ORDER:
        item = raw_modes.get(mode)
        if not isinstance(item, dict):
            continue
        status = str(item.get("status", "UNKNOWN"))
        modes[mode] = {"status": status if status in MODE_STATUSES else "UNKNOWN", "controlDetected": bool(item.get("controlDetected")), "controllable": bool(item.get("controllable"))}
    result.update({"status": "PASS" if probe_status == 200 else "UI_PROBE_FAILED", "error_code": None if probe_status == 200 else "UI_PROBE_FAILED", "modes": modes})
    _write_state(result, state_path)
    return result


def _available(availability: dict[str, Any] | None, mode: str) -> bool:
    if not isinstance(availability, dict):
        return False
    item = availability.get(mode)
    if isinstance(item, str):
        return item == "AVAILABLE"
    return isinstance(item, dict) and item.get("status") == "AVAILABLE" and item.get("controllable", True) is not False


def select_deepseek_mode(task_text: str, *, codex_mode: str = "CUSTOM_DEEPSEEK_TEXT_ONLY", tools_policy: str = "strict_reject", user_preference: str = "auto", explicit_model_alias: str | None = None, availability: dict[str, Any] | None = None, search_allowed: bool = True, thinking_allowed: bool = True, expert_allowed: bool = True) -> dict[str, Any]:
    """Select a probed mode; never claims an unavailable UI mode is ready."""
    preference = (user_preference or "auto").lower().replace("固定", "").replace("模式", "")
    aliases = {value: key for key, value in MODE_ALIASES.items()}
    # A user-fixed mode is the strongest instruction. An explicit model alias
    # is considered only when no fixed preference was supplied; AUTO then uses
    # task intent and the read-only availability snapshot.
    fixed_preference = preference if preference in MODE_ALIASES and preference != "auto" else None
    explicit_mode = aliases.get(explicit_model_alias or "") if explicit_model_alias else None
    explicit_mode = explicit_mode or {"deepseek-web-fast": "normal", "deepseek-head": "expert"}.get(explicit_model_alias or "")
    requested = fixed_preference or explicit_mode or "auto"
    if requested != "auto":
        if _available(availability, requested):
            why = "用户手动固定模式" if fixed_preference else "显式模型别名"
            selected_alias = mode_alias(requested)
            if explicit_mode and not fixed_preference and explicit_model_alias in {"deepseek-web-fast", "deepseek-head"}:
                selected_alias = explicit_model_alias
            return {"selected_mode": requested, "selected_model_alias": selected_alias, "why_selected": why, "fallback_reason": None, "mode_available": True, "codex_mode": codex_mode, "tools_policy": tools_policy}
        unavailable_alias = explicit_model_alias if explicit_mode and not fixed_preference and explicit_model_alias in {"deepseek-web-fast", "deepseek-head"} else mode_alias(requested)
        return {"selected_mode": requested, "selected_model_alias": unavailable_alias, "why_selected": "请求了不可用的 DeepSeek 网页模式", "fallback_reason": "DEEPSEEK_MODE_UNAVAILABLE", "mode_available": False, "codex_mode": codex_mode, "tools_policy": tools_policy}
    text = (task_text or "").lower()
    desired = "normal"
    why = "短问答或轻量任务"
    if search_allowed and any(term.lower() in text for term in SEARCH_TERMS):
        desired, why = "search", "包含搜索/最新/网页意图"
    elif thinking_allowed and any(term.lower() in text for term in THINKING_TERMS):
        desired, why = "thinking", "复杂分析/架构/审计任务"
    elif expert_allowed and any(term.lower() in text for term in EXPERT_TERMS):
        desired, why = "expert", "复杂工程/debug任务"
    if _available(availability, desired):
        return {"selected_mode": desired, "selected_model_alias": mode_alias(desired), "why_selected": why, "fallback_reason": None, "mode_available": True, "codex_mode": codex_mode, "tools_policy": tools_policy}
    if desired != "normal" and _available(availability, "normal"):
        return {"selected_mode": "normal", "selected_model_alias": mode_alias("normal"), "why_selected": "目标模式未通过 UI 探测，回退普通模式", "fallback_reason": desired.upper() + "_MODE_UNAVAILABLE", "mode_available": True, "codex_mode": codex_mode, "tools_policy": tools_policy}
    return {"selected_mode": desired, "selected_model_alias": mode_alias(desired), "why_selected": why, "fallback_reason": "DEEPSEEK_MODE_UNAVAILABLE", "mode_available": False, "codex_mode": codex_mode, "tools_policy": tools_policy}


if __name__ == "__main__":
    print(json.dumps(probe_deepseek_modes(), ensure_ascii=False))

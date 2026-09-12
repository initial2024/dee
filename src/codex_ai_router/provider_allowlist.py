"""Explicit local provider allowlist for Codex-integrated routing."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .deepseek_modes import load_probe_state


DEEPSEEK_BASE = "http://127.0.0.1:8792/v1"
DEEPSEEK_HEALTH = "http://127.0.0.1:8791/health"
DEEPSEEK_DIRECT_BASE = "http://127.0.0.1:8791"
DEEPSEEK_DIRECT_HEALTH = DEEPSEEK_DIRECT_BASE + "/health"
LOCAL_BASE = "http://127.0.0.1:1234/v1"
BRIDGE_API_KEY_ENV = "XIAOYU_ROUTER_BRIDGE_API_KEY"
DEEPSEEK_KNOWN_ALIASES = {"deepseek-head", "deepseek-web", "deepseek-web-fast", "deepseek-web-search", "deepseek-web-thinking", "deepseek-web-quick-thinking", "deepseek-web-expert", "deepseek-web-expert-max-review", "deepseek-web-auto"}


def _loopback(value: str) -> bool:
    return value.startswith("http://127.0.0.1:") or value.startswith("http://localhost:")


def deepseek_bridge_direct_allowed(endpoint: str = DEEPSEEK_DIRECT_BASE) -> bool:
    """Allow the direct brain only on its fixed local Browser Bridge socket.

    This is deliberately separate from the Worker-backed bridge provider: it
    carries no Worker API key, advertises no public models, and is used only by
    the local coordinator/agent paths.
    """
    normalized = str(endpoint).rstrip("/")
    return normalized in {"http://127.0.0.1:8791", "http://localhost:8791"}


def bridge_api_key_present() -> bool:
    """Return only presence metadata; never expose the local key value."""
    return bool(os.getenv(BRIDGE_API_KEY_ENV, "").strip())


def _configured() -> dict[str, Any]:
    path = Path(os.getenv("XIAOYU_ROUTER_PROVIDER_CONFIG", str(Path.home() / ".codex-ai-router" / "providers.json")))
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _health(url: str, timeout: float = 1.0) -> tuple[str, dict[str, Any]]:
    if not _loopback(url):
        return "ERROR", {}
    try:
        with urlopen(Request(url, method="GET"), timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
            return ("BUSY" if isinstance(body, dict) and body.get("busy") is True else "ENABLED"), body if isinstance(body, dict) else {}
    except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError):
        return "OFFLINE", {}


def _deepseek_ready_models() -> list[str]:
    state = load_probe_state()
    modes = state.get("modes") if isinstance(state.get("modes"), dict) else {}
    ready: list[str] = []
    available = lambda name: isinstance(modes.get(name), dict) and modes[name].get("status") == "AVAILABLE" and modes[name].get("controllable", True) is not False
    if available("quick"):
        ready.extend(["deepseek-web", "deepseek-web-fast", "deepseek-web-auto"])
    if available("expert"):
        ready.extend(["deepseek-web-expert", "deepseek-head"])
    if available("expert") and available("thinking"):
        ready.extend(["deepseek-web-thinking", "deepseek-web-quick-thinking", "deepseek-web-expert-thinking"])
    if available("quick") and available("search"):
        ready.append("deepseek-web-search")
    if available("expert") and available("thinking") and available("search"):
        ready.append("deepseek-web-expert-thinking-search")
    if available("vision") and available("expert") and available("thinking"):
        ready.append("deepseek-web-vision-expert-thinking")
    if available("file") and available("expert") and available("thinking"):
        ready.append("deepseek-web-file-extract")
    if ready:
        ready.append("deepseek-web-auto")
    return list(dict.fromkeys(ready))


def providers() -> list[dict[str, Any]]:
    """Return provider metadata without exposing key values."""
    bridge_state, _ = _health(DEEPSEEK_HEALTH)
    bridge_key_present = bridge_api_key_present()
    if bridge_state == "ENABLED" and not bridge_key_present:
        bridge_state = "AUTH_MISSING"
    probe_state = load_probe_state()
    probe_status = str(probe_state.get("status") or "")
    if bridge_state == "OFFLINE" and probe_status in {"LOGIN_REQUIRED", "CAPTCHA_REQUIRED", "ACCOUNT_RISK", "RATE_LIMITED", "UI_PROBE_FAILED"}:
        bridge_state = probe_status
    deepseek_models = _deepseek_ready_models() if bridge_state == "ENABLED" else []
    if bridge_state == "ENABLED" and not deepseek_models:
        probe_status = str(probe_state.get("status") or "UI_PROBE_FAILED")
        bridge_state = probe_status if probe_status != "PASS" else "UI_PROBE_FAILED"
    records: list[dict[str, Any]] = [{
        "id": "deepseek-web-bridge", "type": "DEEPSEEK_WEB_BRIDGE", "endpoint": DEEPSEEK_BASE,
        "models": deepseek_models, "known_models": sorted(DEEPSEEK_KNOWN_ALIASES), "enabled": True, "status": bridge_state,
        "api_key_env": BRIDGE_API_KEY_ENV, "api_key_present": bridge_key_present,
        "mode_probe": probe_state.get("status", "UNKNOWN"), "mode_availability": probe_state.get("modes", {}),
    }]
    direct_state, _ = _health(DEEPSEEK_DIRECT_HEALTH)
    records.append({
        "id": "deepseek-bridge-direct",
        "type": "DEEPSEEK_BRIDGE_DIRECT_INTERNAL",
        "endpoint": DEEPSEEK_DIRECT_BASE,
        "models": [],
        "enabled": deepseek_bridge_direct_allowed(),
        "status": direct_state,
        "scope": "LOCAL_COORDINATOR_ONLY",
        "api_only_exposed": False,
        "fallback_eligible": False,
    })
    local_state, _ = _health(os.getenv("XIAOYU_LOCAL_MODEL_HEALTH", LOCAL_BASE + "/models"))
    records.append({"id": "local-model", "type": "LOCAL_MODEL", "endpoint": os.getenv("XIAOYU_LOCAL_MODEL_BASE", LOCAL_BASE), "models": ["local-light"], "enabled": True, "status": local_state})
    for provider_id, entry in (_configured().get("providers", {}) or {}).items():
        if not isinstance(entry, dict) or entry.get("type") not in {"external_api", "external_openai_compatible", "custom_external_api"}:
            continue
        allowlisted = entry.get("allowlisted") is True or entry.get("allowlist") is True
        enabled = entry.get("enabled") is True
        if not allowlisted:
            continue
        key_env = str(entry.get("api_key_env", ""))
        key_present = bool(key_env and os.getenv(key_env))
        records.append({"id": provider_id, "type": "EXTERNAL_API_ALLOWED", "endpoint": str(entry.get("base_url", "")), "wire_api": str(entry.get("wire_api") or "chat_completions"), "models": [str(value) for value in entry.get("models", []) if isinstance(value, str)], "enabled": enabled, "status": "ENABLED" if enabled and key_present else ("AUTH_MISSING" if enabled else "DISABLED"), "api_key_env": key_env, "api_key_env_configured": bool(key_env), "api_key_present": key_present})
    records.append({"id": "manual-plan", "type": "MANUAL_PLAN", "endpoint": None, "models": ["manual-plan"], "enabled": True, "status": "ENABLED"})
    return records


def model_catalog() -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    snapshot = providers()
    for provider in snapshot:
        if not provider["enabled"] or provider["status"] in {"DISABLED", "OFFLINE", "AUTH_MISSING", "BUSY", "ERROR"}:
            continue
        for model in provider["models"]:
            result.append({"id": model, "object": "model", "owned_by": "xiaoyu-router", "provider": provider["id"], "provider_type": provider["type"]})
    healthy = [item for item in snapshot if item["enabled"] and item["status"] == "ENABLED"]
    if healthy:
        result.append({"id": "hybrid-agent", "object": "model", "owned_by": "xiaoyu-router", "provider": "hybrid", "provider_type": "HYBRID_AGENT"})
    return result


def resolve(model: str) -> tuple[dict[str, Any] | None, str | None]:
    for provider in providers():
        if model in provider["models"]:
            if not provider["enabled"]:
                return None, "PROVIDER_DISABLED"
            if provider["status"] == "AUTH_MISSING":
                return provider, "AUTH_MISSING"
            if provider["status"] == "BUSY":
                return provider, "BRIDGE_BUSY"
            if provider["status"] in {"OFFLINE", "ERROR"}:
                return provider, "BRIDGE_OFFLINE" if provider["type"] == "DEEPSEEK_WEB_BRIDGE" else "LOCAL_MODEL_OFFLINE"
            return provider, None
        if provider.get("type") == "DEEPSEEK_WEB_BRIDGE" and model in provider.get("known_models", []):
            return provider, "DEEPSEEK_MODE_UNAVAILABLE"
    if model == "hybrid-agent":
        for provider in providers():
            if provider["enabled"] and provider["status"] == "ENABLED" and provider["type"] in {"DEEPSEEK_WEB_BRIDGE", "LOCAL_MODEL", "EXTERNAL_API_ALLOWED"}:
                return provider, None
        return None, "NO_HEALTHY_PROVIDER"
    return None, "MODEL_NOT_ALLOWLISTED"


if __name__ == "__main__":
    print(json.dumps({"providers": providers(), "models": model_catalog()}, ensure_ascii=False))

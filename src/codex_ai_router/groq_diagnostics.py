"""Non-secret Groq eligibility gates used by the local Router and UI."""
from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from .providers.base import ProviderError
from .providers.groq import GroqProvider
from .providers.runtime_models import RuntimeModelState


def _models(entry: Mapping[str, Any]) -> list[str]:
    registry = entry.get("model_registry")
    if not isinstance(registry, Mapping):
        return []
    values = registry.get("ALLOWED_MODELS", registry.get("USABLE_MODELS", []))
    return [value for value in values if isinstance(value, str) and value]


def _runtime_pass_model(provider_id: str, candidates: list[str], state: RuntimeModelState) -> str | None:
    records = state.data.get("providers", {}).get(provider_id, {})
    passed = [model for model in candidates if isinstance(records.get(model), dict) and records[model].get("status") == "PASS" and not state.recent_timeout(provider_id, model)]
    if not passed:
        return None
    return min(passed, key=lambda model: float(records[model].get("elapsed_seconds", float("inf"))))


def diagnose(provider_id: str, entry: Mapping[str, Any] | None, state: RuntimeModelState | None = None, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Return only non-secret state. This function never invokes Groq."""
    entry = entry or {}
    env = environ if environ is not None else os.environ
    runtime = state or RuntimeModelState()
    legacy_enabled = entry.get("enabled") is True
    allowlist_enabled = entry.get("allowlisted") is True or entry.get("allowlist") is True
    key_env = entry.get("api_key_env") if isinstance(entry.get("api_key_env"), str) else ""
    auth_present = bool(key_env and env.get(key_env))
    candidates = _models(entry)
    preferred = entry.get("preferred_runtime_model") if isinstance(entry.get("preferred_runtime_model"), str) else None
    upstream_model = preferred if preferred in candidates else (candidates[0] if candidates else None)
    runtime_model = _runtime_pass_model(provider_id, candidates, runtime)
    if not legacy_enabled:
        eligibility_reason = "LEGACY_PROVIDER_DISABLED"
    elif not auth_present:
        eligibility_reason = "AUTH_MISSING"
    elif not upstream_model:
        eligibility_reason = "UPSTREAM_MODEL_UNCONFIRMED"
    elif not runtime_model:
        eligibility_reason = "EXTERNAL_MODEL_NOT_ELIGIBLE"
    elif not allowlist_enabled:
        eligibility_reason = "EXTERNAL_PROVIDER_NOT_ALLOWLIST_ENABLED"
    else:
        eligibility_reason = "READY"
    runtime_eligible = eligibility_reason == "READY"
    error_code = "" if runtime_eligible else ("EXTERNAL_PROVIDER_NOT_ALLOWLIST_ENABLED" if not allowlist_enabled else eligibility_reason)
    return {
        "provider": provider_id,
        "legacy_enabled": legacy_enabled,
        "allowlist_enabled": allowlist_enabled,
        "auth_present": auth_present,
        "key_visible": False,
        "model_alias": f"xiaoyu-api-{provider_id}",
        "upstream_model": upstream_model,
        "runtime_eligible": runtime_eligible,
        "eligibility_reason": eligibility_reason,
        "live_request_allowed": runtime_eligible,
        "error_code": error_code,
    }


def _live_error(error: ProviderError) -> str:
    return {
        "GROQ_AUTH_ERROR": "AUTH_FAILED",
        "GROQ_PERMISSION_ERROR": "AUTH_FAILED",
        "GROQ_MODEL_UNAVAILABLE": "MODEL_NOT_FOUND",
        "GROQ_RATE_LIMIT": "RATE_LIMITED",
        "GROQ_UPSTREAM_FORMAT_ERROR": "UPSTREAM_FORMAT_ERROR",
        "GROQ_UPSTREAM_HTTP_503": "UPSTREAM_HTTP_503",
        "GROQ_TIMEOUT": "UPSTREAM_HTTP_503",
        "GROQ_NETWORK_ERROR": "UPSTREAM_HTTP_503",
        "GROQ_UNKNOWN_ERROR": "UPSTREAM_FORMAT_ERROR",
    }.get(str(error), "UPSTREAM_FORMAT_ERROR")


def live_smoke(provider_id: str, entry: Mapping[str, Any] | None, confirmed: bool, state: RuntimeModelState | None = None, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Run one bounded request only after every local gate is satisfied."""
    diagnosis = diagnose(provider_id, entry, state=state, environ=environ)
    if not confirmed:
        return {**diagnosis, "live_request_sent": False, "content_detected": False, "normalized": False, "error_code": "LIVE_CONFIRMATION_REQUIRED"}
    if not diagnosis["allowlist_enabled"]:
        return {**diagnosis, "live_request_sent": False, "content_detected": False, "normalized": False, "error_code": "EXTERNAL_PROVIDER_NOT_ALLOWLIST_ENABLED"}
    if not diagnosis["runtime_eligible"]:
        return {**diagnosis, "live_request_sent": False, "content_detected": False, "normalized": False, "error_code": diagnosis["error_code"] or "EXTERNAL_MODEL_NOT_ELIGIBLE"}
    assert diagnosis["upstream_model"]
    key_env = str((entry or {}).get("api_key_env", "GROQ_API_KEY"))
    provider = GroqProvider(model=diagnosis["upstream_model"], key_env=key_env, provider_id=provider_id)
    try:
        response = provider.complete("只回复 GROQ_OK", max_tokens=8)
        return {**diagnosis, "live_request_sent": True, "content_detected": bool(response.text.strip()), "normalized": bool(response.text.strip()), "error_code": ""}
    except ProviderError as error:
        return {**diagnosis, "live_request_sent": True, "content_detected": False, "normalized": False, "error_code": _live_error(error)}

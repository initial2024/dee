"""Explicit, opt-in brain adapters for the Xiaoyu Local Agent.

The execution layer never invokes a provider while merely creating a plan.
Callers must explicitly request ``invoke_brain=True``.  Adapters return text
only; credentials, headers, prompts, and provider responses are not logged by
this module.  DeepSeek is reached only through the existing loopback Browser
Bridge, while external providers are selected from the local allowlist.
"""
from __future__ import annotations

import re
from typing import Any, Callable

from .deepseek_head import run_deepseek_head
from .deepseek_bridge_direct import run_deepseek_bridge_direct
from .provider_allowlist import providers as allowlisted_providers
from .providers.base import ProviderError
from .providers.local_backend import LocalBackend
from .providers.openai_compatible import OpenAICompatibleProvider
from .response_compat import extract_visible_text


class BrainProviderError(RuntimeError):
    """Stable error returned to the UI without exposing provider details."""

    def __init__(self, code: str, *, original_error_code: str | None = None, metadata: dict[str, Any] | None = None):
        safe_code = _safe_error_code(code)
        original = _safe_error_code(original_error_code or safe_code)
        super().__init__(safe_code)
        self.code = safe_code
        self.original_error_code = original
        self.metadata = {"original_error_code": original, **(metadata or {})}


def _safe_error_code(value: object) -> str:
    code = str(value or "BRAIN_PROVIDER_ERROR").strip().upper()
    return code if re.fullmatch(r"[A-Z0-9][A-Z0-9_.:-]{0,127}", code) else "BRAIN_PROVIDER_ERROR"


def _provider_error(code: str, original: str | None = None, **metadata: Any) -> BrainProviderError:
    return BrainProviderError(code, original_error_code=original or code, metadata=metadata)


def _map_local_error(original: str) -> str:
    code = original.upper()
    if code == "LOCAL_DIRECT_BACKEND_NOT_CONFIGURED":
        return "LOCAL_BACKEND_NOT_CONFIGURED"
    if code in {"LOCAL_EMPTY_RESPONSE", "UPSTREAM_CONTENT_EMPTY", "EMPTY_RESPONSE"}:
        return "LOCAL_EMPTY_RESPONSE"
    if "TIMEOUT" in code:
        return "LOCAL_MODEL_TIMEOUT"
    if code in {"LOCAL_MODEL_OFFLINE", "LOCAL_UNAVAILABLE", "LLAMA_SERVER_NOT_FOUND", "NO_GGUF_MODEL", "MODEL_NOT_SELECTED", "MODEL_NOT_FOUND"}:
        return "LOCAL_MODEL_OFFLINE"
    return "LOCAL_MODEL_ERROR"


def _map_deepseek_error(original: str) -> str:
    code = original.upper()
    if code in {"BRIDGE_OFFLINE", "BRIDGE_UNREACHABLE", "BRIDGE_NOT_LISTENING", "DEEPSEEK_BRIDGE_OFFLINE"}:
        return "DEEPSEEK_BRIDGE_OFFLINE"
    if code == "DEEPSEEK_BRIDGE_DIRECT_UNAVAILABLE":
        return code
    if code in {"LOGIN_REQUIRED", "DEEPSEEK_LOGIN_REQUIRED", "UPSTREAM_HTTP_401"}:
        return "DEEPSEEK_LOGIN_REQUIRED"
    if code in {"BRIDGE_BUSY", "DEEPSEEK_BRIDGE_BUSY"}:
        return "DEEPSEEK_BRIDGE_BUSY"
    if code in {"UPSTREAM_CONTENT_EMPTY", "DEEPSEEK_EMPTY_RESPONSE"}:
        return "DEEPSEEK_EMPTY_RESPONSE"
    if code in {
        "DEEPSEEK_MODE_UNAVAILABLE",
        "PROVIDER_PRE_SEND_ERROR",
        "DEEPSEEK_BRIDGE_DIRECT_PAYLOAD_INVALID",
        "DEEPSEEK_BRIDGE_DIRECT_MODE_PARAM_INVALID",
        "DEEPSEEK_BRIDGE_DIRECT_CONTEXT_BUILD_FAILED",
        "DEEPSEEK_BRIDGE_DIRECT_ALLOWLIST_BLOCKED",
        "DEEPSEEK_BRIDGE_DIRECT_URL_NOT_CONFIGURED",
        "DEEPSEEK_BRIDGE_DIRECT_REQUEST_NOT_SENT",
        "LLM_CONTEXT_BUNDLE_BUILD_FAILED",
        "SANITIZER_BLOCKED_BEFORE_SEND",
    }:
        return code
    if code == "BRIDGE_REQUEST_FAILED":
        return "DEEPSEEK_BRIDGE_ERROR"
    return "DEEPSEEK_PROVIDER_ERROR"


def _map_external_error(original: str) -> str:
    code = original.upper()
    if code in {"EXTERNAL_PROVIDER_NOT_ALLOWLIST_ENABLED", "EXTERNAL_PROVIDER_AUTH_MISSING"}:
        return code
    if code.startswith("UPSTREAM_HTTP_") or code in {"UPSTREAM_CONTENT_EMPTY", "AUTH_MISSING", "AUTH_FAILED", "RATE_LIMITED", "MODEL_NOT_FOUND"}:
        return code
    return "BRAIN_PROVIDER_ERROR"


_REAL_SECRET_VALUE = re.compile(
    r"(?ix)(?:\bsk-[a-z0-9_-]{12,}\b|\bbearer\s+[a-z0-9._~-]{12,}\b|"
    r"\beyj[a-z0-9_-]{8,}\.[a-z0-9_-]{8,}\.[a-z0-9_-]{8,}\b|"
    r"\b(?:api[_ -]?key|authorization|token|cookie|password|secret)\s*[:=]\s*(?!\[(?:REDACTED|VALUE_REDACTED)\])[^\s,;]{16,})"
)
_HIGH_RISK_INTENT = re.compile(
    r"(?ix)(?:"
    r"(?:print|display|show|export|leak|reveal|copy|dump|读取|打印|显示|导出|泄露|复制)\s*(?:(?:this|that|the|这个|该)\s*)?(?:api[_ -]?key|authorization|token|cookie|password|secret|密钥|凭据|密码|令牌|存储状态)|"
    r"(?:bypass|绕过).{0,24}(?:captcha|验证码|风控)|"
    r"(?:replay|重放).{0,32}(?:private\s*api|私有\s*api)|"
    r"(?:export|导出).{0,24}(?:storage[_ -]?state|存储状态)"
    r")"
)
_SENSITIVE_FIELD_NAME = re.compile(r"(?i)(?:\b(api[_ -]?key|authorization|bearer|token|cookie|storage[_ -]?state|secret|password)\b|\bsk-(?![A-Za-z0-9_-]))")


def contains_high_risk_intent(task: str) -> bool:
    """Block dangerous requests, while allowing sanitized structure analysis."""
    value = str(task or "")
    # A statement such as “不泄露密钥” is a safety constraint, not an
    # exfiltration request.  Remove only explicit negated actions before the
    # intent test; concrete secret values remain independently blocked.
    value = re.sub(r"(?i)(?:不|不要|禁止|无需|未|not|do\s+not)\s*(?:print|display|show|export|leak|reveal|copy|dump|读取|打印|显示|导出|泄露|复制|输出)", "", value)
    return bool(_HIGH_RISK_INTENT.search(value))


def contains_real_secret_value(task: str) -> bool:
    return bool(_REAL_SECRET_VALUE.search(task or ""))


def _sanitize_llm_task(task: str) -> str:
    return _SENSITIVE_FIELD_NAME.sub("credential_field_redacted", str(task or "").strip())


def _prompt(task: str) -> str:
    """Build a bounded advisory prompt without asking the brain to execute."""
    return (
        "你是小羽 Local Agent 的计划大脑。只能输出分析和候选计划，不能调用工具，"
        "不能声称已读取、修改、运行、提交或部署。不要输出或索要任何凭据或敏感配置。"
        "所有写入、测试和 commit 都必须等待人工确认。\n\n"
        "用户任务：\n" + _sanitize_llm_task(task)
    )


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        try:
            return extract_visible_text(value).strip()
        except Exception:
            return ""
    return ""


def _external_provider() -> OpenAICompatibleProvider:
    """Create one explicitly enabled external provider without exposing its key."""
    records = allowlisted_providers()
    eligible = [
        item for item in records
        if isinstance(item, dict)
        and item.get("type") == "EXTERNAL_API_ALLOWED"
        and item.get("enabled") is True
        and item.get("status") == "ENABLED"
        and isinstance(item.get("endpoint"), str)
        and item.get("endpoint")
        and isinstance(item.get("models"), list)
        and item.get("models")
    ]
    if not eligible:
        raise _provider_error("EXTERNAL_PROVIDER_NOT_ALLOWLIST_ENABLED")
    item = eligible[0]
    key_env = str(item.get("api_key_env") or "")
    if not key_env:
        raise _provider_error("EXTERNAL_PROVIDER_AUTH_MISSING")
    return OpenAICompatibleProvider(
        base_url=str(item["endpoint"]),
        model=str(item["models"][0]),
        key_env=key_env,
        provider_id=str(item.get("id") or "external-allowed"),
        wire_api=str(item.get("wire_api") or "chat_completions"),
    )


def invoke_brain(
    provider: str,
    task: str,
    *,
    local_backend: LocalBackend | None = None,
    deepseek_runner: Callable[..., dict[str, Any]] = run_deepseek_head,
    deepseek_direct_runner: Callable[..., dict[str, Any]] = run_deepseek_bridge_direct,
    external_factory: Callable[[], OpenAICompatibleProvider] = _external_provider,
    selected_mode: str | None = None,
    search: bool | None = None,
) -> str:
    """Invoke one explicitly selected brain and return advisory text.

    This function is deliberately dependency-injectable for tests.  The
    default LocalAgent path does not call it; a caller must opt in.
    """
    if contains_real_secret_value(task) or contains_high_risk_intent(task):
        raise _provider_error("LOCAL_AGENT_HIGH_RISK_STOP")
    prompt = _prompt(task)
    try:
        if provider == "local-light":
            result = (local_backend or LocalBackend()).auto_ask(prompt, mode="auto", risk="low")
            text = _text(result)
        elif provider == "deepseek-head":
            result = deepseek_runner(prompt, task_type="AUTO")
            if str(result.get("status")) != "PASS":
                original = str(result.get("error_code") or result.get("status") or "BRIDGE_REQUEST_FAILED")
                raise _provider_error(_map_deepseek_error(original), original)
            text = _text(result.get("analysis") or result)
        elif provider == "deepseek-bridge-direct":
            try:
                result = deepseek_direct_runner(prompt, task_type="AUTO", selected_mode=selected_mode, search=search)
            except TypeError as exc:
                # Preserve compatibility with injected/test runners that use
                # the original two-argument callable contract.  The built-in
                # adapter still receives selected_mode/search above.
                if "unexpected keyword argument" not in str(exc):
                    raise
                result = deepseek_direct_runner(prompt, task_type="AUTO")
            if str(result.get("status")) != "PASS":
                original = str(result.get("error_code") or result.get("status") or "DEEPSEEK_BRIDGE_OFFLINE")
                raise _provider_error(
                    _map_deepseek_error(original),
                    original,
                    provider_error_stage=str(result.get("provider_error_stage") or "before_bridge_send"),
                    bridge_send_attempted=str(result.get("bridge_send_attempted") or "NO"),
                    bridge_ui_send_attempt_count=int(result.get("bridge_ui_send_attempt_count") or 0),
                    model_output_available=str(result.get("model_output_available") or "NO"),
                )
            text = _text(result.get("analysis") or result)
        elif provider == "external-allowed":
            text = _text(external_factory().ask(prompt))
        elif provider == "hybrid-agent":
            snapshot = allowlisted_providers()
            healthy = {
                str(item.get("type")): item
                for item in snapshot
                if isinstance(item, dict) and item.get("enabled") is True and item.get("status") == "ENABLED"
            }
            if "LOCAL_MODEL" in healthy:
                text = invoke_brain("local-light", task, local_backend=local_backend, deepseek_runner=deepseek_runner, deepseek_direct_runner=deepseek_direct_runner, external_factory=external_factory, selected_mode=selected_mode, search=search)
            elif "DEEPSEEK_WEB_BRIDGE" in healthy:
                text = invoke_brain("deepseek-head", task, local_backend=local_backend, deepseek_runner=deepseek_runner, deepseek_direct_runner=deepseek_direct_runner, external_factory=external_factory, selected_mode=selected_mode, search=search)
            elif "EXTERNAL_API_ALLOWED" in healthy:
                text = invoke_brain("external-allowed", task, local_backend=local_backend, deepseek_runner=deepseek_runner, deepseek_direct_runner=deepseek_direct_runner, external_factory=external_factory, selected_mode=selected_mode, search=search)
            else:
                raise _provider_error("NO_HEALTHY_BRAIN_PROVIDER")
        else:
            raise _provider_error("BRAIN_PROVIDER_INVALID")
    except BrainProviderError:
        raise
    except ProviderError as exc:
        code = str(exc)
        if provider == "local-light":
            raise _provider_error(_map_local_error(code), code) from exc
        if provider in {"deepseek-head", "deepseek-bridge-direct"}:
            raise _provider_error(_map_deepseek_error(code), code) from exc
        if provider == "external-allowed":
            raise _provider_error(_map_external_error(code), code) from exc
        raise _provider_error("BRAIN_PROVIDER_ERROR", code) from exc
    except Exception as exc:
        if provider in {"deepseek-head", "deepseek-bridge-direct"}:
            raise _provider_error(
                "PROVIDER_PRE_SEND_ERROR",
                "DEEPSEEK_BRIDGE_DIRECT_REQUEST_NOT_SENT",
                provider_error_stage="before_bridge_send",
                bridge_send_attempted="NO",
                bridge_ui_send_attempt_count=0,
                model_output_available="NO",
            ) from exc
        raise _provider_error("BRAIN_PROVIDER_ERROR", type(exc).__name__) from exc
    if not text:
        if provider == "local-light":
            raise _provider_error("LOCAL_EMPTY_RESPONSE", "UPSTREAM_CONTENT_EMPTY")
        raise _provider_error("UPSTREAM_CONTENT_EMPTY")
    return text


__all__ = ["BrainProviderError", "invoke_brain"]

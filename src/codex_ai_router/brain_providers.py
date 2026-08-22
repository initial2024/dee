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
from .provider_allowlist import providers as allowlisted_providers
from .providers.base import ProviderError
from .providers.local_backend import LocalBackend
from .providers.openai_compatible import OpenAICompatibleProvider
from .response_compat import extract_visible_text


class BrainProviderError(RuntimeError):
    """Stable error returned to the UI without exposing provider details."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


_SENSITIVE_TASK = re.compile(
    r"api[_ -]?key|authorization|bearer|cookie|token|password|secret|密钥|凭据|密码|令牌|绕过|captcha|验证码|风控",
    re.IGNORECASE,
)


def _prompt(task: str) -> str:
    """Build a bounded advisory prompt without asking the brain to execute."""
    return (
        "你是小羽 Local Agent 的计划大脑。只能输出分析和候选计划，不能调用工具，"
        "不能声称已读取、修改、运行、提交或部署。不要输出或索要密码、token、cookie、"
        "Authorization、API key 或其他凭据。所有写入、测试和 commit 都必须等待人工确认。\n\n"
        "用户任务：\n" + task.strip()
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
        raise BrainProviderError("EXTERNAL_PROVIDER_NOT_ALLOWLIST_ENABLED")
    item = eligible[0]
    key_env = str(item.get("api_key_env") or "")
    if not key_env:
        raise BrainProviderError("EXTERNAL_PROVIDER_AUTH_MISSING")
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
    external_factory: Callable[[], OpenAICompatibleProvider] = _external_provider,
) -> str:
    """Invoke one explicitly selected brain and return advisory text.

    This function is deliberately dependency-injectable for tests.  The
    default LocalAgent path does not call it; a caller must opt in.
    """
    if _SENSITIVE_TASK.search(task or ""):
        raise BrainProviderError("LOCAL_AGENT_HIGH_RISK_STOP")
    prompt = _prompt(task)
    try:
        if provider == "local-light":
            result = (local_backend or LocalBackend()).auto_ask(prompt, mode="auto", risk="low")
            text = _text(result)
        elif provider == "deepseek-head":
            result = deepseek_runner(prompt, task_type="AUTO")
            if str(result.get("status")) != "PASS":
                raise BrainProviderError(str(result.get("error_code") or result.get("status") or "BRIDGE_REQUEST_FAILED"))
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
                text = invoke_brain("local-light", task, local_backend=local_backend, deepseek_runner=deepseek_runner, external_factory=external_factory)
            elif "DEEPSEEK_WEB_BRIDGE" in healthy:
                text = invoke_brain("deepseek-head", task, local_backend=local_backend, deepseek_runner=deepseek_runner, external_factory=external_factory)
            elif "EXTERNAL_API_ALLOWED" in healthy:
                text = invoke_brain("external-allowed", task, local_backend=local_backend, deepseek_runner=deepseek_runner, external_factory=external_factory)
            else:
                raise BrainProviderError("NO_HEALTHY_BRAIN_PROVIDER")
        else:
            raise BrainProviderError("BRAIN_PROVIDER_INVALID")
    except BrainProviderError:
        raise
    except ProviderError as exc:
        code = str(exc)
        if provider == "local-light":
            raise BrainProviderError("LOCAL_MODEL_OFFLINE" if "UNAVAILABLE" in code or "NOT_FOUND" in code else "LOCAL_MODEL_ERROR") from exc
        raise BrainProviderError("BRAIN_PROVIDER_ERROR") from exc
    except Exception as exc:
        raise BrainProviderError("BRAIN_PROVIDER_ERROR") from exc
    if not text:
        raise BrainProviderError("UPSTREAM_CONTENT_EMPTY")
    return text


__all__ = ["BrainProviderError", "invoke_brain"]

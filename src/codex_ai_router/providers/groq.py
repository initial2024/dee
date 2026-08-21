from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Any

from .base import BaseProvider, DiscoveredModel, ProviderError, ProviderResponse


def classify_groq_error(exc: Exception) -> str:
    status = getattr(exc, "status_code", None)
    if status == 401: return "GROQ_AUTH_ERROR"
    if status == 403: return "GROQ_PERMISSION_ERROR"
    if status == 429: return "GROQ_RATE_LIMIT"
    if status == 404: return "GROQ_MODEL_UNAVAILABLE"
    if status in {400, 422}: return "GROQ_UPSTREAM_FORMAT_ERROR"
    if status == 503: return "GROQ_UPSTREAM_HTTP_503"
    name = type(exc).__name__.lower()
    if "timeout" in name: return "GROQ_TIMEOUT"
    if any(value in name for value in ("connection", "network", "api_connection")): return "GROQ_NETWORK_ERROR"
    return "GROQ_UNKNOWN_ERROR"


def groq_messages(value: object) -> list[dict[str, str]]:
    if isinstance(value, str): return [{"role": "user", "content": value}]
    messages: list[dict[str, str]] = []
    if isinstance(value, list):
        for item in value:
            if not isinstance(item, dict): continue
            role = item.get("role") if isinstance(item.get("role"), str) else "user"
            content = item.get("content", "")
            if isinstance(content, list):
                content = "\n".join(part.get("text", "") for part in content if isinstance(part, dict) and isinstance(part.get("text"), str))
            if isinstance(content, str) and content: messages.append({"role": role, "content": content})
    return messages or [{"role": "user", "content": ""}]


class GroqProvider(BaseProvider):
    """Official Groq-SDK adapter; no custom headers are sent or stored."""
    name = "groq"

    def __init__(self, model: str | None = None, key_env: str = "GROQ_API_KEY", provider_id: str = "groq", timeout: int = 20):
        self.model, self.key_env, self.provider_id, self.timeout = model or "", key_env, provider_id, timeout
        self.model_discovery_supported, self.remote_model_list_status = "UNKNOWN", "UNKNOWN"

    def _client(self):
        if not (key := os.getenv(self.key_env)): raise ProviderError("GROQ_AUTH_ERROR")
        try:
            from groq import Groq
        except ImportError as exc: raise ProviderError("GROQ_SDK_NOT_INSTALLED") from exc
        return Groq(api_key=key, timeout=self.timeout)

    def available(self) -> bool: return bool(os.getenv(self.key_env))

    @staticmethod
    def _values(response: Any) -> list[Any]:
        if hasattr(response, "data"): return list(response.data or [])
        if isinstance(response, dict): return list(response.get("data", response.get("models", [])) or [])
        return list(response or [])

    @staticmethod
    def _field(item: Any, name: str) -> Any:
        return item.get(name) if isinstance(item, dict) else getattr(item, name, None)

    def discover_models(self, refresh: bool = False) -> list[DiscoveredModel]:
        del refresh
        try:
            response = self._client().models.list(); now = datetime.now(timezone.utc)
            records = [DiscoveredModel(self.provider_id, str(model_id), str(model_id), self._field(item, "owned_by"), {"source": "GROQ_SDK", "validation": "REMOTE_LIST"}, now) for item in self._values(response) if (model_id := self._field(item, "id") or self._field(item, "name"))]
            self.model_discovery_supported = "YES"; self.remote_model_list_status = "PASS"; return records
        except ProviderError:
            self.remote_model_list_status = "GROQ_AUTH_ERROR"
            self.model_discovery_supported = "GROQ_AUTH_ERROR"
            return []
        except Exception as exc:
            self.remote_model_list_status = classify_groq_error(exc); self.model_discovery_supported = self.remote_model_list_status; return []

    def refresh_models(self) -> list[DiscoveredModel]: return self.discover_models(True)
    def models(self) -> list[str]: return [item.model_id for item in self.discover_models()]
    def candidate_models(self) -> list[str]: return self.models()

    def complete(self, prompt: object, max_tokens: int = 16) -> ProviderResponse:
        if not self.model: raise ProviderError("MODEL_NOT_CONFIGURED")
        try:
            response = self._client().chat.completions.create(model=self.model, messages=groq_messages(prompt), max_tokens=min(max(1, max_tokens), 16), temperature=0)
            choices = getattr(response, "choices", None) or (response.get("choices", []) if isinstance(response, dict) else [])
            if not choices: raise ProviderError("GROQ_UNKNOWN_ERROR")
            choice = choices[0]; message = getattr(choice, "message", None) or (choice.get("message", {}) if isinstance(choice, dict) else {})
            text = getattr(message, "content", None) or (message.get("content") if isinstance(message, dict) else None)
            if not isinstance(text, str) or not text.strip(): raise ProviderError("GROQ_UNKNOWN_ERROR")
            usage = getattr(response, "usage", None)
            if hasattr(usage, "model_dump"): usage = usage.model_dump()
            return ProviderResponse(text, self.model, getattr(choice, "finish_reason", None), usage if isinstance(usage, dict) else None, "groq_chat_completions")
        except ProviderError: raise
        except Exception as exc: raise ProviderError(classify_groq_error(exc)) from exc

    def ask(self, prompt: str) -> str: return self.complete(prompt).text

    def probe(self, model: str, timeout: int = 20) -> dict:
        previous, self.model, self.timeout = self.model, model, min(timeout, 20); started = time.monotonic()
        try:
            response = self.complete("只回复 GROQ_OK", max_tokens=8)
            status, text = "PASS", bool(response.text.strip())
            return {"model": model, "status": status, "error_class": "", "assistant_text": text, "elapsed_seconds": round(time.monotonic() - started, 3)}
        except ProviderError as exc:
            return {"model": model, "status": "FAIL", "error_class": str(exc), "assistant_text": False, "elapsed_seconds": round(time.monotonic() - started, 3)}
        finally: self.model, self.timeout = previous, timeout

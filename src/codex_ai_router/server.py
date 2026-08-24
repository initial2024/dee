from __future__ import annotations

import html
import json
import os
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from . import provider_config
from .classifier import classify
from .network import NetworkMode, NetworkState
from .policy import FastLocalGate, FastLocalPolicy
from .providers import LMStudioProvider, OpenAICompatibleProvider, GroqProvider
from .providers.local_backend import LocalBackend
from .providers.openai_compatible import ResponsesResponseAdapter
from .providers.base import ProviderError
from .providers.runtime_models import RuntimeModelState, text_candidates
from .call_records import append as append_call_record
from .provider_allowlist import BRIDGE_API_KEY_ENV, model_catalog, providers as allowlisted_providers, resolve as resolve_allowlisted
from .deepseek_modes import select_deepseek_mode
from .deepseek_mode_switch import ModeSwitchProxyError, proxy_mode_switch
from .response_compat import ResponseCompatibilityError, diagnostic_headers, extract_visible_text, iter_normalized_sse, normalize_chat_completion
from .tools_policy import MANUAL_PLAN, STRICT_REJECT, TEXT_ONLY_STRIP, TEXT_ONLY_SYSTEM_INSTRUCTION, load_policy, request_has_tools, strip_tool_fields, normalize_policy
from .vision import VisionProxy
from .agent_api import AGENT_API_PORT, AgentApiController, MAX_AGENT_BODY_BYTES


VIRTUAL_MODELS = ("xiaoyu-auto", "xiaoyu-local", "xiaoyu-api-auto", "xiaoyu-api-local", "xiaoyu-lightboat")


class ToolsPolicyError(RuntimeError):
    """A safe, user-facing tool capability error with no request-body details."""

    def __init__(self, code: str, message: str):
        super().__init__(code)
        self.code = code
        self.message = message


def provider_virtual_model(provider_id: str) -> str: return "xiaoyu-api-" + provider_id


def _input_text(value: object) -> str:
    if isinstance(value, str):
        return value
    pieces: list[str] = []
    if isinstance(value, dict):
        return _input_text(value.get("content", value.get("text", "")))
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str):
                pieces.append(item)
            elif isinstance(item, dict):
                content = item.get("content", item.get("text", ""))
                if isinstance(content, str):
                    pieces.append(content)
                elif isinstance(content, list):
                    pieces.append(_input_text(content))
    return "\n".join(pieces)


def _compact_codex_envelope(text: str) -> str:
    """Drop Codex transport context while preserving the trailing user text."""
    normalized = html.unescape(text).strip()
    markers = ("</environment_context>", "</app-context>", "</permissions instructions>")
    marker, cut = max(((item, normalized.rfind(item)) for item in markers), key=lambda pair: pair[1])
    if cut >= 0:
        tail = normalized[cut + len(marker):].strip()
        if tail:
            return tail
    return normalized


def _last_user_text(value: object) -> str:
    """Return only the last user message, never system/developer context."""
    if isinstance(value, str):
        return _compact_codex_envelope(value)
    if isinstance(value, dict):
        return _compact_codex_envelope(_input_text(value))
    if not isinstance(value, list):
        return ""
    user_texts: list[str] = []
    fallback_texts: list[str] = []
    for item in value:
        if isinstance(item, dict):
            text = _input_text(item)
            if not text:
                continue
            fallback_texts.append(text)
            if item.get("role") == "user":
                user_texts.append(text)
        elif isinstance(item, str) and item:
            fallback_texts.append(item)
    selected = (user_texts or fallback_texts)[-1] if (user_texts or fallback_texts) else ""
    return _compact_codex_envelope(selected)


def _bounded_output_tokens(payload: dict, maximum: int = 16) -> int | None:
    """Preserve the caller's small output cap without ever increasing it."""
    value = payload.get("max_output_tokens", payload.get("max_tokens"))
    if value is None:
        return None
    try:
        return max(1, min(int(value), maximum))
    except (TypeError, ValueError):
        raise RuntimeError("INVALID_MAX_OUTPUT_TOKENS")


def _has_image(value: object) -> bool:
    raw = json.dumps(value, ensure_ascii=False).lower()
    return "input_image" in raw or "image_url" in raw


def _allowlisted_headers(provider: dict[str, object], *, stream: bool = False) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if stream:
        headers["Accept"] = "text/event-stream"
    provider_type = provider.get("type")
    if provider_type == "EXTERNAL_API_ALLOWED":
        key_env = provider.get("api_key_env")
        if not isinstance(key_env, str) or not os.getenv(key_env):
            raise RuntimeError("AUTH_MISSING")
        headers["Authorization"] = "Bearer " + os.environ[key_env]
    elif provider_type == "DEEPSEEK_WEB_BRIDGE":
        # The local Worker intentionally requires its own sk-* API key.  Keep
        # this loopback-only credential out of records, UI, and error details.
        key = os.getenv(BRIDGE_API_KEY_ENV, "").strip()
        if not key:
            raise RuntimeError("AUTH_MISSING")
        headers["Authorization"] = "Bearer " + key
    return headers


class RouterService:
    """Secret-free local Responses facade for a Codex custom provider."""
    def __init__(self, network_mode: NetworkMode = NetworkMode.AUTO, local: object | None = None, fast_local_policy: FastLocalPolicy | None = None, runtime_models: RuntimeModelState | None = None, tools_policy: str | None = None):
        self.network = NetworkState(network_mode)
        self.local = local or LocalBackend(lmstudio=LMStudioProvider())
        self.fast_local_policy = fast_local_policy or FastLocalPolicy()
        self.vision = VisionProxy()
        self.runtime_models = runtime_models or RuntimeModelState()
        self.tools_policy = normalize_policy(tools_policy) if tools_policy is not None else load_policy()

    def models(self) -> list[dict]:
        names = [*VIRTUAL_MODELS, *(provider_virtual_model(provider_id) for provider_id, _ in self._provider_entries())]
        existing = [{"id": name, "object": "model", "owned_by": "xiaoyu-router"} for name in names]
        seen = {item["id"] for item in existing}
        return existing + [item for item in model_catalog() if item["id"] not in seen]

    def _provider_entries(self) -> list[tuple[str, dict]]:
        records = provider_config.load().get("providers", {})
        values = [(key, value) for key, value in records.items() if isinstance(value, dict) and value.get("enabled", True) and value.get("type") in {"openai_compatible", "custom_openai_compatible", "groq"}]
        return sorted(values, key=lambda item: int(item[1].get("priority", 100)))

    def _metadata_candidates(self, entry: dict) -> list[str]:
        snapshot = entry.get("model_registry", {}) if isinstance(entry.get("model_registry"), dict) else {}
        values = snapshot.get("ALLOWED_MODELS", snapshot.get("USABLE_MODELS", entry.get("last_discovery_models", [])))
        denied = set(entry.get("denied_model_ids", []))
        return [model for model in values if isinstance(model, str) and model not in denied] if isinstance(values, list) else []

    def _api_provider(self, requested: str) -> tuple[str, object, dict]:
        entries = self._provider_entries()
        if requested == "xiaoyu-lightboat":
            entries = [item for item in entries if item[0].startswith("lightboat")]
        elif requested.startswith("xiaoyu-api-") and requested != "xiaoyu-api-auto":
            provider_id = requested.removeprefix("xiaoyu-api-")
            entries = [item for item in entries if item[0] == provider_id]
        elif requested == "xiaoyu-api-auto":
            ranked: list[tuple[int, int, float, str, dict]] = []
            for provider_id, entry in entries:
                selected = self._select_text_model_for_entry(provider_id, entry)
                if selected:
                    elapsed = self.runtime_models.data.get("providers", {}).get(provider_id, {}).get(selected, {}).get("elapsed_seconds", float("inf"))
                    priority = int(entry.get("priority", 100)); model_priority = int(entry.get("model_priorities", {}).get(selected, 100))
                    ranked.append((priority, model_priority, float(elapsed), provider_id, entry))
            entries = [(provider_id, entry) for _, _, _, provider_id, entry in sorted(ranked)]
        if not entries:
            raise RuntimeError("API_PROVIDER_UNAVAILABLE")
        provider_id, entry = entries[0]
        return provider_id, self._provider_for_entry(provider_id, entry), entry

    @staticmethod
    def _provider_for_entry(provider_id: str, entry: dict) -> object:
        return GroqProvider(key_env=entry.get("api_key_env", "GROQ_API_KEY"), provider_id=provider_id, timeout=int(entry.get("request_timeout", 20))) if entry.get("type") == "groq" else OpenAICompatibleProvider(
            entry.get("base_url"), key_env=entry.get("api_key_env", ""), wire_api=entry.get("wire_api", "responses"),
            requires_bearer_auth=entry.get("requires_bearer_auth", False), header_env=entry.get("headers", {}),
            provider_id=provider_id, provider_metadata=entry, model_env=entry.get("model_env"), timeout=int(entry.get("request_timeout", 60)),
        )

    def _select_text_model_for_entry(self, provider_id: str, entry: dict) -> str | None:
        candidates, _ = text_candidates(self._metadata_candidates(entry))
        preferred = entry.get("preferred_runtime_model")
        if isinstance(preferred, str) and preferred in candidates and not self.runtime_models.recent_timeout(provider_id, preferred):
            details = self.runtime_models.data.get("providers", {}).get(provider_id, {}).get(preferred, {})
            if details.get("status") == "PASS": return preferred
        passed = [model for model in candidates if self.runtime_models.data.get("providers", {}).get(provider_id, {}).get(model, {}).get("status") == "PASS" and not self.runtime_models.recent_timeout(provider_id, model)]
        priorities = entry.get("model_priorities", {}) if isinstance(entry.get("model_priorities"), dict) else {}
        return min(passed, key=lambda model: (int(priorities.get(model, 100)), float(self.runtime_models.data["providers"][provider_id][model].get("elapsed_seconds", float("inf"))), model)) if passed else None

    def explain_selection(self, virtual_model: str) -> dict:
        provider_id, _provider, entry = self._api_provider(virtual_model)
        model = self._select_text_model_for_entry(provider_id, entry)
        runtime = self.runtime_models.data.get("providers", {})
        provider_runtime = runtime.get(provider_id, {}) if isinstance(runtime, dict) else {}
        denied = set(entry.get("denied_model_ids", []))
        skipped: list[dict] = []
        snapshot = entry.get("model_registry", {}) if isinstance(entry.get("model_registry"), dict) else {}
        discovered = snapshot.get("DISCOVERED_MODELS", [])
        candidates, image_models = text_candidates(self._metadata_candidates(entry))
        for candidate in discovered if isinstance(discovered, list) else []:
            if candidate in denied:
                skipped.append({"provider": provider_id, "model": candidate, "reason": "POLICY_DENIED"})
            elif candidate in image_models:
                skipped.append({"provider": provider_id, "model": candidate, "reason": "IMAGE_MODEL"})
            elif candidate not in candidates:
                skipped.append({"provider": provider_id, "model": candidate, "reason": "NOT_ELIGIBLE"})
            else:
                details = provider_runtime.get(candidate, {}) if isinstance(provider_runtime, dict) else {}
                status = details.get("status")
                if self.runtime_models.recent_timeout(provider_id, candidate):
                    skipped.append({"provider": provider_id, "model": candidate, "reason": "TIMEOUT_COOLDOWN"})
                elif status != "PASS":
                    skipped.append({"provider": provider_id, "model": candidate, "reason": "NOT_RUNTIME_PASS"})
        preferred = entry.get("preferred_runtime_model")
        reason = "manual_preferred" if model and model == preferred else "priority_then_runtime_latency"
        skipped_providers: list[dict] = []
        if virtual_model == "xiaoyu-api-auto":
            for other_id, other_entry in self._provider_entries():
                if other_id == provider_id:
                    continue
                other_model = self._select_text_model_for_entry(other_id, other_entry)
                skipped_providers.append({
                    "provider": other_id,
                    "model": other_model,
                    "reason": "LOWER_PRIORITY_OR_LATENCY" if other_model else "NO_RUNTIME_PASS_OR_UNAVAILABLE",
                })
        return {
            "virtual_model": virtual_model,
            "provider": provider_id,
            "provider_priority": entry.get("priority", 100),
            "selected_model": model,
            "manual_preferred": preferred,
            "denied_models": entry.get("denied_model_ids", []),
            "reason": reason,
            "why_selected": "preferred model is allowed and runtime PASS" if reason == "manual_preferred" else "allowed runtime PASS model ranked by priority then latency",
            "skipped": skipped,
            "skipped_providers": skipped_providers,
        }

    def _fast_candidates(self) -> tuple[list[tuple[str, object, dict, str]], list[dict]]:
        """Select only proven responsive text models; never rediscover or probe here."""
        ranked: list[tuple[int, float, int, int, str, object, dict, str]] = []
        skipped: list[dict] = []
        for provider_id, entry in self._provider_entries():
            snapshot = entry.get("model_registry", {}) if isinstance(entry.get("model_registry"), dict) else {}
            denied = set(entry.get("denied_model_ids", []))
            for model in snapshot.get("DISCOVERED_MODELS", []):
                if model in denied:
                    skipped.append({"provider": provider_id, "model": model, "reason": "POLICY_DENIED"})
            candidates, image_models = text_candidates(self._metadata_candidates(entry))
            if image_models:
                skipped.append({"provider": provider_id, "models": image_models, "reason": "IMAGE_MODEL"})
            viable: list[tuple[float, int, str]] = []
            for model in candidates:
                details = self.runtime_models.data.get("providers", {}).get(provider_id, {}).get(model, {})
                if self.runtime_models.recent_timeout(provider_id, model):
                    skipped.append({"provider": provider_id, "model": model, "reason": "TIMEOUT_COOLDOWN"})
                elif details.get("status") == "PASS":
                    viable.append((float(details.get("elapsed_seconds", float("inf"))), int(entry.get("model_priorities", {}).get(model, 100)), model))
                else:
                    skipped.append({"provider": provider_id, "model": model, "reason": "NOT_RUNTIME_PASS"})
            if not viable:
                continue
            latency, model_priority, model = min(viable)
            # A proven Groq path is preferred for short delegated summaries;
            # all other providers remain ordered by observed latency and policy priority.
            groq_rank = 0 if entry.get("type") == "groq" else 1
            ranked.append((groq_rank, latency, int(entry.get("priority", 100)), model_priority, provider_id, self._provider_for_entry(provider_id, entry), entry, model))
        ranked.sort(key=lambda item: item[:4])
        return [(provider_id, provider, entry, model) for _, _, _, _, provider_id, provider, entry, model in ranked], skipped

    def explain_fast_delegation(self) -> dict:
        candidates, skipped = self._fast_candidates()
        selected = candidates[0] if candidates else None
        return {
            "selected_provider": selected[0] if selected else None,
            "selected_model": selected[3] if selected else None,
            "why_selected": "runtime_pass_text_model; groq_preferred_then_latency" if selected else "no runtime PASS text model",
            "skipped": skipped,
        }

    def _select_text_model(self, provider: object, entry: dict) -> str:
        selected = self._select_text_model_for_entry(provider.provider_id, entry)
        if not selected and not self._metadata_candidates(entry):
            candidates, _ = text_candidates(provider.candidate_models())
            selected = self.runtime_models.select(provider.provider_id, candidates)
        if not selected:
            raise RuntimeError("DOWNSTREAM_UNAVAILABLE")
        return selected

    def _invoke_selected_api(self, request_payload: dict, virtual_model: str, provider: object, entry: dict, selected: str) -> dict:
        if isinstance(provider, GroqProvider):
            try:
                provider.model = selected
                result = provider.complete(request_payload.get("input"), max_tokens=_bounded_output_tokens(request_payload) or 16)
                return self._response(virtual_model, result.text, result.usage)
            except ProviderError as exc:
                raise RuntimeError(str(exc)) from exc
        outbound = {"input": _input_text(request_payload.get("input"))}
        if isinstance(request_payload.get("instructions"), str):
            outbound["instructions"] = request_payload["instructions"]
        limit = _bounded_output_tokens(request_payload)
        if limit is not None:
            token_field = str(provider.provider_metadata.get("responses_token_limit_field", "max_output_tokens"))
            if token_field not in {"max_output_tokens", "max_tokens"}:
                raise RuntimeError("INVALID_TOKEN_LIMIT_FIELD")
            outbound[token_field] = limit
        outbound.update({"model": selected, "stream": False})
        request = Request(provider.endpoint(), data=json.dumps(outbound).encode("utf-8"), headers=provider.request_headers(content_type=True), method="POST")
        try:
            with urlopen(request, timeout=provider.timeout) as response:
                if response.headers.get_content_type() == "text/html":
                    raise RuntimeError("NON_API_RESPONSE")
                raw = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise RuntimeError(f"DOWNSTREAM_HTTP_{exc.code}") from exc
        except TimeoutError as exc:
            raise RuntimeError(f"DOWNSTREAM_TIMEOUT:{provider.timeout}") from exc
        except (URLError, json.JSONDecodeError) as exc:
            raise RuntimeError("DOWNSTREAM_UNAVAILABLE") from exc
        text = ResponsesResponseAdapter.text(raw)
        if not text:
            raise RuntimeError("UPSTREAM_CONTENT_EMPTY")
        return self._response(virtual_model, text, raw.get("usage"))

    def delegate_fast_readonly(self, prompt: str, max_seconds: int = 60) -> dict:
        """A bounded, direct route for bridge summaries; the localhost server is optional."""
        if not self.network.remote_allowed:
            return {"ok": False, "summary": "Remote delegation is disabled.", "provider": None, "model": None, "error_code": "REMOTE_FALLBACK_DISABLED_OFFLINE", "skipped": []}
        started = time.monotonic(); candidates, skipped = self._fast_candidates()
        if not candidates:
            return {"ok": False, "summary": "No policy-eligible runtime PASS text provider is available.", "provider": None, "model": None, "error_code": "NO_AVAILABLE_PROVIDER", "skipped": skipped}
        for provider_id, provider, entry, model in candidates:
            remaining = max_seconds - (time.monotonic() - started)
            if remaining <= 0:
                break
            previous_timeout = getattr(provider, "timeout", None)
            if previous_timeout is not None:
                provider.timeout = max(1, min(int(previous_timeout), int(remaining)))
            try:
                response = self._invoke_selected_api({"input": prompt, "max_output_tokens": 64}, provider_virtual_model(provider_id), provider, entry, model)
                self.runtime_models.record(provider_id, model, "PASS", time.monotonic() - started, "HTTP_200")
                return {"ok": True, "summary": response["output_text"], "provider": provider_id, "model": model, "error_code": None, "skipped": skipped}
            except RuntimeError as exc:
                code = str(exc)
                state = "TIMEOUT" if "TIMEOUT" in code else "FAIL"
                self.runtime_models.record(provider_id, model, state, time.monotonic() - started, code)
                skipped.append({"provider": provider_id, "model": model, "reason": code})
            finally:
                if previous_timeout is not None:
                    provider.timeout = previous_timeout
        final = "ALL_PROVIDERS_TIMEOUT" if any(item.get("reason", "").find("TIMEOUT") >= 0 for item in skipped) else "NO_AVAILABLE_PROVIDER"
        return {"ok": False, "summary": "No delegated provider completed within the configured budget.", "provider": None, "model": None, "error_code": final, "skipped": skipped}

    def _local_response(self, prompt: str, virtual_model: str, mode: str = "auto") -> dict:
        if not self.local.available():
            raise RuntimeError("LOCAL_UNAVAILABLE")
        auto_ask = getattr(self.local, "auto_ask", None)
        try:
            text = auto_ask(prompt, mode=mode) if callable(auto_ask) else self.local.ask(prompt)
        except Exception as exc:
            code = str(exc) or "LOCAL_DIRECT_BACKEND_ERROR"
            if code == "LOCAL_EMPTY_RESPONSE":
                raise RuntimeError(code) from exc
            if code == "LOCAL_HIGH_RISK_SAFE_STOP":
                raise RuntimeError(code) from exc
            raise RuntimeError(code) from exc
        return self._response(virtual_model, text)

    def _api_response(self, request_payload: dict, virtual_model: str) -> dict:
        if not self.network.remote_allowed:
            raise RuntimeError("REMOTE_FALLBACK_DISABLED_OFFLINE")
        _, provider, entry = self._api_provider(virtual_model)
        # AUTO makes a short reachability decision per invocation.  A later
        # invocation probes again, so recovery needs no VPN-specific logic.
        if isinstance(provider, OpenAICompatibleProvider) and self.network.mode is NetworkMode.AUTO and self.network.probe(provider.models_endpoint(), timeout=2.0) == "OFFLINE_OR_REMOTE_UNAVAILABLE":
            raise RuntimeError("REMOTE_UNAVAILABLE")
        selected = self._select_text_model(provider, entry)
        return self._invoke_selected_api(request_payload, virtual_model, provider, entry, selected)

    @staticmethod
    def _response(model: str, text: str, usage: dict | None = None) -> dict:
        # Codex's Responses SSE parser treats ``usage`` as an optional typed
        # object.  An empty object is not equivalent to an omitted value: it
        # fails deserialization because the required token counters are
        # missing, after which Codex reconnects even though text was shown.
        response_usage: dict | None = None
        required_usage = ("input_tokens", "output_tokens", "total_tokens")
        if isinstance(usage, dict) and all(key in usage for key in required_usage):
            try:
                response_usage = {
                    "input_tokens": int(usage["input_tokens"]),
                    "output_tokens": int(usage["output_tokens"]),
                    "total_tokens": int(usage["total_tokens"]),
                }
                for key in ("input_tokens_details", "output_tokens_details"):
                    if isinstance(usage.get(key), dict):
                        response_usage[key] = usage[key]
            except (TypeError, ValueError):
                response_usage = None
        return {"id": "resp_xiaoyu_" + uuid.uuid4().hex, "object": "response", "created_at": int(time.time()), "status": "completed", "model": model, "output": [{"id": "msg_xiaoyu_" + uuid.uuid4().hex, "type": "message", "status": "completed", "role": "assistant", "content": [{"type": "output_text", "text": text, "annotations": []}]}], "output_text": text, "usage": response_usage}

    @staticmethod
    def _allowlisted_models() -> set[str]:
        return {model for provider in allowlisted_providers() for model in provider.get("models", [])} | {"hybrid-agent"}

    @staticmethod
    def _tools_requested(request_payload: dict) -> bool:
        return request_has_tools(request_payload)

    def _prepare_tools_request(self, request_payload: dict, *, chat: bool = False) -> tuple[dict, bool]:
        """Apply the explicit local tool policy without fabricating tool calls."""
        if self.tools_policy == MANUAL_PLAN:
            return request_payload, True
        if not self._tools_requested(request_payload):
            return request_payload, False
        if self.tools_policy == STRICT_REJECT:
            raise ToolsPolicyError(
                "TOOLS_NOT_SUPPORTED_BY_BACKEND",
                "当前后端不支持工具调用。请切换 OFFICIAL_DIRECT，或启用 TEXT_ONLY 兼容模式。",
            )
        return strip_tool_fields(request_payload, chat=chat), False

    @staticmethod
    def _manual_plan(prompt: str) -> str:
        return "\n".join([
            "Problem Summary: " + prompt,
            "Analysis: Manual review is required; no tool was executed.",
            "Proposed Plan: Inspect the relevant files, run bounded tests, and review the diff.",
            "Codex Instruction: Review this plan and execute only approved, reversible steps.",
            "Manual Commands: None generated automatically.",
            "Risk Check: No file, network, or provider mutation was performed.",
            "Requires Official Codex Tools: YES",
            "Requires User Confirmation: YES",
        ])

    @staticmethod
    def _allowlisted_endpoint(provider: dict[str, object]) -> str:
        endpoint = provider.get("endpoint")
        if not isinstance(endpoint, str) or not endpoint.startswith(("http://", "https://")):
            raise RuntimeError("INVALID_PROVIDER_ENDPOINT")
        return endpoint.rstrip("/") + "/chat/completions"

    def _allowlisted_response(self, request_payload: dict, model: str) -> dict:
        started = time.monotonic()
        request_id = "req_xiaoyu_" + uuid.uuid4().hex
        provider, error = resolve_allowlisted(model)
        if error:
            append_call_record({"id": request_id, "mode": "NO_QUOTA_CODEX_MODE", "model": model, "provider": provider.get("id") if provider else None, "provider_type": provider.get("type") if provider else None, "status": "ERROR", "duration_ms": 0, "error_code": error, "content_detected": False, "normalized": False})
            raise RuntimeError(error)
        assert provider is not None
        is_deepseek_bridge = provider.get("type") == "DEEPSEEK_WEB_BRIDGE"
        prompt_source = request_payload.get("input", request_payload.get("messages", []))
        prompt = _last_user_text(prompt_source) if is_deepseek_bridge else _input_text(prompt_source)
        if not prompt:
            raise RuntimeError("INPUT_REQUIRED")
        if provider.get("type") == "MANUAL_PLAN":
            text = self._manual_plan(prompt)
            append_call_record({"id": request_id, "mode": "MANUAL_PLAN", "model": model, "provider": provider.get("id"), "provider_type": provider.get("type"), "status": "PASS", "duration_ms": int((time.monotonic() - started) * 1000), "error_code": None, "content_detected": True, "normalized": True})
            return self._response(model, text)
        target_model = model
        if model == "hybrid-agent":
            target_model = str((provider.get("models") or ["deepseek-web"])[0])
        selected_mode = None
        if is_deepseek_bridge and model == "deepseek-web-auto":
            selection = select_deepseek_mode(prompt, availability=provider.get("mode_availability"))
            if not selection["mode_available"]:
                raise RuntimeError("DEEPSEEK_MODE_UNAVAILABLE")
            target_model = str(selection["selected_model_alias"])
            selected_mode = selection["selected_mode"]
        # The browser bridge must receive one compact user message. Passing
        # Codex instructions/environment context makes the visible web prompt
        # enormous and can cause downstream clients to reconnect.
        if is_deepseek_bridge:
            outbound_messages = [{"role": "user", "content": prompt}]
        else:
            outbound_messages = []
            instructions = request_payload.get("instructions")
            if isinstance(instructions, str) and instructions.strip():
                outbound_messages.append({"role": "system", "content": instructions})
            outbound_messages.append({"role": "user", "content": prompt})
        outbound: dict[str, object] = {"model": target_model, "messages": outbound_messages, "stream": False}
        headers = _allowlisted_headers(provider)
        request = Request(self._allowlisted_endpoint(provider), data=json.dumps(outbound).encode("utf-8"), headers=headers, method="POST")
        status = "ERROR"; error_code: str | None = None; content_detected = False
        try:
            with urlopen(request, timeout=30) as upstream:
                status_code = int(getattr(upstream, "status", 200))
                raw = json.loads(upstream.read().decode("utf-8"))
            normalized = normalize_chat_completion(raw, model=model)
            content_detected = bool(normalized["choices"][0]["message"].get("content"))
            status = "PASS"
            record = {"id": request_id, "mode": "NO_QUOTA_CODEX_MODE", "model": model, "provider": provider.get("id"), "provider_type": provider.get("type"), "status": status, "duration_ms": int((time.monotonic() - started) * 1000), "error_code": None, "content_detected": content_detected, "normalized": True}
            if selected_mode: record["deepseek_selected_mode"] = selected_mode
            append_call_record(record)
            return self._response(model, normalized["choices"][0]["message"]["content"], normalized.get("usage"))
        except HTTPError as exc:
            error_code = "UPSTREAM_HTTP_" + str(exc.code)
            raise RuntimeError(error_code) from exc
        except ResponseCompatibilityError as exc:
            error_code = str(exc)
            raise RuntimeError(error_code) from exc
        except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            error_code = "UPSTREAM_UNAVAILABLE"
            raise RuntimeError(error_code) from exc
        finally:
            if status != "PASS":
                append_call_record({"id": request_id, "mode": "NO_QUOTA_CODEX_MODE", "model": model, "provider": provider.get("id"), "provider_type": provider.get("type"), "status": status, "duration_ms": int((time.monotonic() - started) * 1000), "error_code": error_code or "UPSTREAM_ERROR", "content_detected": content_detected, "normalized": False})

    def chat_completion(self, request_payload: dict) -> dict:
        model = request_payload.get("model")
        if not isinstance(model, str):
            raise RuntimeError("MODEL_REQUIRED")
        request_payload, manual_plan = self._prepare_tools_request(request_payload, chat=True)
        if manual_plan and model != "manual-plan":
            return normalize_chat_completion(self._response(model, self._manual_plan(_input_text(request_payload.get("messages", [])))), model=model)
        if model in self._allowlisted_models():
            response = self._allowlisted_response({**request_payload, "input": request_payload.get("messages", request_payload.get("input"))}, model)
        else:
            response = self.respond({**request_payload, "input": request_payload.get("messages", request_payload.get("input"))})
        return normalize_chat_completion(response, model=model)

    def stream_chat(self, request_payload: dict):
        model = request_payload.get("model")
        if not isinstance(model, str):
            raise RuntimeError("MODEL_REQUIRED")
        request_payload, manual_plan = self._prepare_tools_request(request_payload, chat=True)
        if manual_plan and model != "manual-plan":
            response = normalize_chat_completion(self._response(model, self._manual_plan(_input_text(request_payload.get("messages", [])))), model=model)
            yield from iter_normalized_sse(["data: " + json.dumps(response, ensure_ascii=False)], model=model)
            return
        if model not in self._allowlisted_models():
            response = self.chat_completion({**request_payload, "stream": False})
            yield from iter_normalized_sse(["data: " + json.dumps(response, ensure_ascii=False)], model=model)
            return
        provider, error = resolve_allowlisted(model)
        if error:
            raise RuntimeError(error)
        assert provider is not None
        if provider.get("type") == "MANUAL_PLAN":
            response = self._allowlisted_response({**request_payload, "input": request_payload.get("messages", request_payload.get("input"))}, model)
            yield from iter_normalized_sse(["data: " + json.dumps(response, ensure_ascii=False)], model=model)
            return
        prompt_source = request_payload.get("messages", request_payload.get("input"))
        prompt = _last_user_text(prompt_source) if provider.get("type") == "DEEPSEEK_WEB_BRIDGE" else _input_text(prompt_source)
        if not prompt:
            raise RuntimeError("INPUT_REQUIRED")
        target_model = model if model != "hybrid-agent" else str((provider.get("models") or ["deepseek-web"])[0])
        if provider.get("type") == "DEEPSEEK_WEB_BRIDGE" and model == "deepseek-web-auto":
            selection = select_deepseek_mode(prompt, availability=provider.get("mode_availability"))
            if not selection["mode_available"]:
                raise RuntimeError("DEEPSEEK_MODE_UNAVAILABLE")
            target_model = str(selection["selected_model_alias"])
        headers = _allowlisted_headers(provider, stream=True)
        request = Request(self._allowlisted_endpoint(provider), data=json.dumps({"model": target_model, "messages": [{"role": "user", "content": prompt}], "stream": True}).encode("utf-8"), headers=headers, method="POST")
        try:
            with urlopen(request, timeout=30) as upstream:
                yield from iter_normalized_sse(upstream, model=model)
        except HTTPError as exc:
            raise RuntimeError("UPSTREAM_HTTP_" + str(exc.code)) from exc

    def respond(self, request_payload: dict) -> dict:
        virtual_model = request_payload.get("model")
        if not isinstance(virtual_model, str):
            raise RuntimeError("MODEL_REQUIRED")
        request_payload, manual_plan = self._prepare_tools_request(request_payload)
        if manual_plan and virtual_model != "manual-plan":
            return self._response(virtual_model, self._manual_plan(_input_text(request_payload.get("input"))))
        if virtual_model in self._allowlisted_models():
            if _has_image(request_payload.get("input")):
                raise RuntimeError("VISION_PROVIDER_UNAVAILABLE")
            return self._allowlisted_response(request_payload, virtual_model)
        if virtual_model not in {model["id"] for model in self.models()}:
            raise RuntimeError("UNKNOWN_VIRTUAL_MODEL")
        if _has_image(request_payload.get("input")):
            outcome = self.vision.route(True, [], self.network.mode is NetworkMode.OFFLINE)
            raise RuntimeError(outcome.status)
        prompt = _input_text(request_payload.get("input"))
        instructions = request_payload.get("instructions")
        if isinstance(instructions, str) and instructions.strip():
            prompt = instructions + "\n\n" + prompt
        if not prompt:
            raise RuntimeError("INPUT_REQUIRED")
        if virtual_model == "xiaoyu-local":
            return self._local_response(prompt, virtual_model, mode="local")
        if virtual_model == "xiaoyu-auto":
            category, risk = classify(prompt)
            if FastLocalGate(self.fast_local_policy).permits(prompt, category, risk) and self.local.available():
                return self._local_response(prompt, virtual_model, mode="auto")
        return self._api_response(request_payload, virtual_model)


class _Handler(BaseHTTPRequestHandler):
    server_version = "XiaoyuRouter/1.1"

    @property
    def service(self) -> RouterService:
        return self.server.service  # type: ignore[attr-defined]

    @property
    def agent_api(self) -> AgentApiController | None:
        return self.server.agent_api  # type: ignore[attr-defined]

    def log_message(self, _format: str, *_args: object) -> None:
        # Never include request bodies or headers in server logs.
        return

    def _send(self, status: int, body: dict, diagnostics: dict[str, str] | None = None) -> None:
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        for name, value in (diagnostics or {}).items(): self.send_header(name, value)
        self.end_headers()
        self.wfile.write(raw)

    def _send_sse(self, body: dict) -> None:
        response_id = body.get("id")
        model = body.get("model")
        created = body.get("created_at")
        text = body.get("output_text") or ""
        output = body.get("output") if isinstance(body.get("output"), list) else []
        output_item = output[0] if output and isinstance(output[0], dict) else {
            "id": response_id + "_msg" if response_id else "msg_xiaoyu",
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "content": [],
        }
        item_id = output_item.get("id") or (response_id + "_msg" if response_id else "msg_xiaoyu")
        content = output_item.get("content") if isinstance(output_item.get("content"), list) else []
        output_part = content[0] if content and isinstance(content[0], dict) else {"type": "output_text", "text": text, "annotations": []}
        empty_part = {**output_part, "text": ""}
        sequence = 0
        events: list[str] = []

        def emit(event_type: str, payload: dict) -> None:
            nonlocal sequence
            sequence += 1
            event = {"type": event_type, "sequence_number": sequence, **payload}
            events.append("event: " + event_type + "\ndata: " + json.dumps(event, ensure_ascii=False) + "\n\n")

        created_response = {**body, "status": "in_progress", "output": [], "output_text": ""}
        emit("response.created", {"response": created_response})
        emit("response.in_progress", {"response": created_response})
        emit("response.output_item.added", {"output_index": 0, "item": {**output_item, "content": []}})
        emit("response.content_part.added", {"item_id": item_id, "output_index": 0, "content_index": 0, "part": empty_part})
        if text:
            emit("response.output_text.delta", {"item_id": item_id, "output_index": 0, "content_index": 0, "delta": text})
        emit("response.output_text.done", {"item_id": item_id, "output_index": 0, "content_index": 0, "text": text})
        emit("response.content_part.done", {"item_id": item_id, "output_index": 0, "content_index": 0, "part": output_part})
        emit("response.output_item.done", {"output_index": 0, "item": output_item})
        emit("response.completed", {"response": body})
        # Responses API streams terminate with response.completed and the
        # HTTP stream ending. ``data: [DONE]`` is a Chat Completions sentinel;
        # sending it here can make Codex treat an otherwise complete response
        # as malformed and retry the same request.
        encoded = "".join(events).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _send_chat_stream(self, payload: dict) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        for frame in self.service.stream_chat(payload):
            self.wfile.write(frame.encode("utf-8"))
            self.wfile.flush()

    def _read_json_payload(self, maximum: int = 2_000_000) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > maximum:
            raise RuntimeError("INVALID_REQUEST_SIZE")
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(payload, dict):
            raise RuntimeError("INVALID_REQUEST")
        return payload

    def do_GET(self) -> None:
        if self.path.startswith("/agent/"):
            if self.agent_api is None:
                self._send(404, {"error": {"code": "not_found"}})
            else:
                status, body = self.agent_api.get(self.path)
                self._send(status, body)
        elif self.path == "/v1/models":
            self._send(200, {"object": "list", "data": self.service.models()})
        elif self.path == "/health":
            self._send(200, {"status": "ok", "localhost_only": True})
        else:
            self._send(404, {"error": {"code": "not_found"}})

    def do_POST(self) -> None:
        if self.path == "/deepseek/mode-switch":
            try:
                payload = self._read_json_payload(8_192)
                status, body = proxy_mode_switch(payload)
                self._send(status, body)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RuntimeError, ModeSwitchProxyError) as exc:
                code = exc.code if isinstance(exc, ModeSwitchProxyError) else "MODE_SWITCH_PAYLOAD_REJECTED"
                self._send(400, {"status": "ERROR", "error_code": code})
            return
        if self.path.startswith("/agent/") or self.path in {"/assist/coordinate", "/deepseek-head/coordinate", "/deepseek-head/context", "/deepseek-head/plan", "/deepseek-head/plan-from-context"}:
            if self.agent_api is None:
                self._send(404, {"error": {"code": "not_found"}})
                return
            try:
                payload = self._read_json_payload(MAX_AGENT_BODY_BYTES)
                status, body = self.agent_api.post(self.path, payload)
                self._send(status, body)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RuntimeError):
                self._send(400, {"status": "ERROR", "error_code": "INVALID_REQUEST"})
            return
        if self.path not in {"/v1/responses", "/v1/chat/completions"}:
            self._send(404, {"error": {"code": "not_found"}})
            return
        try:
            payload = self._read_json_payload()
            if self.path == "/v1/chat/completions":
                if payload.get("stream") is True:
                    self._send_chat_stream(payload)
                    return
                response = self.service.chat_completion(payload)
                self._send(200, response, diagnostic_headers(upstream_status=200, upstream_content_type="application/json", endpoint_mode="ROUTER_CHAT_COMPLETIONS", normalized=True, content_detected=bool(response["choices"][0]["message"].get("content")), stream_mode="non_stream"))
            else:
                response = self.service.respond(payload)
                if payload.get("stream") is True:
                    self._send_sse(response)
                else:
                    self._send(200, response, diagnostic_headers(upstream_status=200, upstream_content_type="application/json", endpoint_mode="ROUTER_RESPONSES", normalized=True, content_detected=bool(response.get("output_text")), stream_mode="non_stream"))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            self._send(400, {"error": {"code": "invalid_json"}})
        except ToolsPolicyError as exc:
            self._send(400, {"error": {"code": exc.code, "message": exc.message}})
        except RuntimeError as exc:
            code = str(exc)
            status = 504 if code.startswith("DOWNSTREAM_TIMEOUT:") else (503 if code in {"LOCAL_UNAVAILABLE", "API_PROVIDER_UNAVAILABLE", "DOWNSTREAM_UNAVAILABLE", "REMOTE_FALLBACK_DISABLED_OFFLINE", "REMOTE_UNAVAILABLE", "BRIDGE_OFFLINE", "BRIDGE_BUSY", "LOCAL_MODEL_OFFLINE", "NO_HEALTHY_PROVIDER", "UPSTREAM_UNAVAILABLE"} else 400)
            self._send(status, {"error": {"code": code}})


class RouterResponsesServer:
    """A localhost-only server. It is intentionally never bound to 0.0.0.0."""
    def __init__(self, service: RouterService | None = None, host: str = "127.0.0.1", port: int = 18789, agent_api: AgentApiController | None = None):
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("LOCALHOST_ONLY")
        self.host, self.port, self.service = host, port, service or RouterService()
        self.httpd = ThreadingHTTPServer((host, port), _Handler)
        self.httpd.service = self.service  # type: ignore[attr-defined]
        # The public serve command only mounts Agent API on its fixed default
        # port.  Tests may pass an explicit controller with port=0; arbitrary
        # custom ports never gain the Agent API implicitly.
        self.httpd.agent_api = agent_api or (AgentApiController(Path.cwd()) if port == AGENT_API_PORT else None)  # type: ignore[attr-defined]
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def serve_forever(self) -> None:
        self.httpd.serve_forever()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        if self.thread:
            self.thread.join(timeout=2)

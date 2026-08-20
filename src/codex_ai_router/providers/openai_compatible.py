from __future__ import annotations

import json, os, time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .base import BaseProvider, DiscoveredModel, ProviderError, ProviderResponse
from .model_discovery import ModelDiscoveryChain


class ResponsesRequestAdapter:
    @staticmethod
    def payload(model: str, prompt: str) -> dict: return {"model": model, "input": prompt}


class ResponsesResponseAdapter:
    @staticmethod
    def text(data: dict) -> str:
        if isinstance(data.get("output_text"), str): return data["output_text"]
        parts: list[str] = []
        for output in data.get("output", []):
            if isinstance(output.get("output_text"), str): parts.append(output["output_text"])
            for content in output.get("content", []):
                value = content.get("text") or content.get("output_text") if isinstance(content, dict) else None
                if isinstance(value, str): parts.append(value)
                elif isinstance(value, dict) and isinstance(value.get("value"), str): parts.append(value["value"])
        return "".join(parts)
    @classmethod
    def normalize(cls, data: dict) -> ProviderResponse:
        text = cls.text(data)
        if not text: raise ProviderError("RESPONSES_TEXT_MISSING")
        return ProviderResponse(text, data.get("model"), data.get("status"), data.get("usage"), "responses")


class OpenAICompatibleProvider(BaseProvider):
    name, VALID_PROTOCOLS, CACHE_TTL_SECONDS = "api", {"chat_completions", "responses"}, 1800
    _model_cache: dict[str, tuple[float, list[DiscoveredModel], str]] = {}

    def __init__(self, base_url: str | None = None, model: str | None = None, key_env: str = "XIAOYU_CODER_API_KEY", timeout: int = 20, wire_api: str | None = None, requires_bearer_auth: bool | None = None, custom_headers: dict[str, str] | None = None, header_env: dict[str, str] | None = None, provider_id: str = "api", provider_metadata: dict | None = None, model_env: str | None = None, codex_profile_path=None):
        self.base_url, self.model = (base_url or os.getenv("XIAOYU_CODER_API_BASE", "")).rstrip("/"), model or os.getenv("XIAOYU_CODER_API_MODEL", "")
        self.key_env, self.provider_id = (os.getenv("XIAOYU_CODER_API_KEY_ENV", key_env) if key_env == "XIAOYU_CODER_API_KEY" else key_env), provider_id
        self.wire_api = (wire_api or os.getenv("XIAOYU_CODER_API_WIRE_API", "chat_completions")).lower()
        if self.wire_api not in self.VALID_PROTOCOLS: raise ValueError("wire_api must be chat_completions or responses")
        setting = os.getenv("XIAOYU_CODER_API_REQUIRES_BEARER_AUTH", "true").lower()
        self.requires_bearer_auth = requires_bearer_auth if requires_bearer_auth is not None else setting in {"1", "true", "yes"}
        self.custom_headers, self.header_env, self.timeout = dict(custom_headers or {}), dict(header_env or self._header_env_from_environment()), timeout
        self.model_discovery_supported = "UNKNOWN"
        self.remote_model_list_status = "UNKNOWN"
        self.provider_metadata = dict(provider_metadata or {})
        self.model_env, self.codex_profile_path = model_env, codex_profile_path
        self.last_model_sources: dict[str, str] = {}
        self._validating_candidate = False

    @staticmethod
    def _header_env_from_environment() -> dict[str, str]:
        try: data = json.loads(os.getenv("XIAOYU_CODER_API_HEADER_ENVS_JSON", "{}"))
        except json.JSONDecodeError: return {}
        return data if isinstance(data, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in data.items()) else {}
    @property
    def normalized_v1_base_url(self) -> str: return self.base_url if self.base_url.endswith("/v1") else self.base_url + "/v1"
    def endpoint(self) -> str: return self.normalized_v1_base_url + "/responses" if self.wire_api == "responses" else self.base_url + "/chat/completions"
    def models_endpoint(self) -> str:
        configured = self.provider_metadata.get("model_discovery_endpoint")
        if isinstance(configured, str) and configured.strip():
            value = configured.strip()
            if value.startswith(("https://", "http://")): return value
            return self.normalized_v1_base_url + "/" + value.lstrip("/")
        return self.normalized_v1_base_url + "/models"
    def available(self) -> bool: return bool(self.base_url and (not self.requires_bearer_auth or os.getenv(self.key_env)))
    def request_headers(self, content_type: bool = False) -> dict[str, str]:
        style = str(self.provider_metadata.get("inference_auth_style", "inherit")).lower()
        if style not in {"inherit", "bearer", "custom_header", "bearer_plus_custom_header"}:
            raise ProviderError("INVALID_INFERENCE_AUTH_STYLE")
        include_custom = style in {"inherit", "custom_header", "bearer_plus_custom_header"}
        include_bearer = style in {"bearer", "bearer_plus_custom_header"} or (style == "inherit" and self.requires_bearer_auth)
        headers = dict(self.custom_headers) if include_custom else {}
        if include_custom:
            for name, env_name in self.header_env.items():
                if value := os.getenv(env_name): headers[name] = value
        if include_bearer:
            if not (key := os.getenv(self.key_env)): raise ProviderError("API_PROVIDER_UNAVAILABLE_OR_MISSING_KEY")
            headers["Authorization"] = "Bearer " + key
        if content_type: headers["Content-Type"] = "application/json"
        return headers

    def discovery_request_headers(self) -> dict[str, str]:
        """Model-list credentials are independent from inference credentials."""
        style = str(self.provider_metadata.get("model_discovery_auth_style", "inherit")).lower()
        headers: dict[str, str] = {}
        mappings = self.provider_metadata.get("model_discovery_headers", {})
        if isinstance(mappings, dict):
            for name, env_name in mappings.items():
                if isinstance(name, str) and isinstance(env_name, str) and (value := os.getenv(env_name)):
                    headers[name] = value
        if style == "inherit":
            return {**self.request_headers(), **headers}
        if style == "bearer":
            if not (key := os.getenv(self.key_env)): raise ProviderError("MODEL_DISCOVERY_MISSING_KEY")
            headers["Authorization"] = "Bearer " + key
        elif style not in {"none", "custom_header"}:
            raise ProviderError("INVALID_MODEL_DISCOVERY_AUTH_STYLE")
        return headers

    def discovery_request(self) -> Request:
        method = str(self.provider_metadata.get("model_discovery_method", "GET")).upper()
        if method not in {"GET", "POST"}: raise ProviderError("INVALID_MODEL_DISCOVERY_METHOD")
        endpoint = self.models_endpoint()
        query = self.provider_metadata.get("model_discovery_query", {})
        if isinstance(query, dict) and query:
            endpoint += ("&" if "?" in endpoint else "?") + urlencode(query)
        body = self.provider_metadata.get("model_discovery_body") if method == "POST" else None
        data = json.dumps(body).encode() if isinstance(body, dict) else None
        headers = self.discovery_request_headers()
        if data is not None: headers["Content-Type"] = "application/json"
        return Request(endpoint, data=data, headers=headers, method=method)

    def _remote_models(self) -> list[DiscoveredModel]:
        """Attempt the standards endpoint without treating its failure as provider failure."""
        try:
            with urlopen(self.discovery_request(), timeout=self.timeout) as response:
                if response.headers.get_content_type() == "text/html": self.remote_model_list_status = "NON_API_RESPONSE"; return []
                payload = json.loads(response.read()); now = datetime.now(timezone.utc)
                models = [DiscoveredModel(self.provider_id, item["id"], item.get("id", ""), item.get("owned_by"), {**item, "source": "REMOTE_MODEL_LIST", "validation": "REMOTE_LIST"}, now) for item in payload.get("data", []) if isinstance(item, dict) and item.get("id")]
                self.remote_model_list_status = "PASS"; return models
        except HTTPError as exc: self.remote_model_list_status = f"HTTP_{exc.code}"; return []
        except (URLError, TimeoutError, json.JSONDecodeError, ProviderError): self.remote_model_list_status = "ERROR"; return []

    def validate_model_candidate(self, model: str) -> bool:
        previous = self.model
        try:
            self.model = model
            self._validating_candidate = True
            return self.complete("Return exactly: API_ROUTER_OK").text.strip() == "API_ROUTER_OK"
        except ProviderError:
            return False
        finally:
            self._validating_candidate = False
            self.model = previous

    def discover_models(self, refresh: bool = False) -> list[DiscoveredModel]:
        candidate_chain = ModelDiscoveryChain(self.provider_id, self.base_url, self.provider_metadata, self.model_env, self.codex_profile_path)
        candidate_signature = tuple(candidate_chain.candidates())
        cache_key = f"{self.provider_id}|{self.models_endpoint()}|{self.requires_bearer_auth}|{candidate_signature}"
        cached = self._model_cache.get(cache_key)
        if cached and not refresh and time.monotonic() - cached[0] < self.CACHE_TTL_SECONDS:
            self.model_discovery_supported = cached[2]; return cached[1]
        models = self._remote_models()
        if models:
            self.model_discovery_supported = "YES"; self._model_cache[cache_key] = (time.monotonic(), models, "YES"); return models
        # Discovery-only callers may explicitly forbid the legacy candidate
        # validation probe, which uses the inference endpoint.
        if self.provider_metadata.get("model_discovery_validate_candidates") is False:
            self.model_discovery_supported = self.remote_model_list_status
            self._model_cache[cache_key] = (time.monotonic(), [], self.model_discovery_supported)
            return []
        candidates = list(candidate_signature)
        validated: list[DiscoveredModel] = []
        for candidate, source in candidates:
            if self.validate_model_candidate(candidate):
                self.last_model_sources[candidate] = source
                validated.append(DiscoveredModel(self.provider_id, candidate, candidate, None, {"source": source, "validation": "PASS"}, datetime.now(timezone.utc)))
        self.model_discovery_supported = "FALLBACK_VALIDATED" if validated else self.remote_model_list_status
        self._model_cache[cache_key] = (time.monotonic(), validated, self.model_discovery_supported)
        return validated
    def refresh_models(self) -> list[DiscoveredModel]: return self.discover_models(refresh=True)
    def models(self) -> list[str]: return [model.model_id for model in self.discover_models()]
    def candidate_models(self) -> list[str]:
        discovered = self.models()
        return discovered or ([self.model] if self.model and self.model_discovery_supported == "NO" else [])

    def complete(self, prompt: str) -> ProviderResponse:
        if not self.available(): raise ProviderError("API_PROVIDER_UNAVAILABLE_OR_MISSING_KEY")
        if not self.model: raise ProviderError("MODEL_NOT_CONFIGURED")
        payload = ResponsesRequestAdapter.payload(self.model, prompt) if self.wire_api == "responses" else {"model": self.model, "messages": [{"role": "user", "content": prompt}], "temperature": 0}
        request = Request(self.endpoint(), data=json.dumps(payload).encode(), headers=self.request_headers(content_type=True))
        try:
            with urlopen(request, timeout=self.timeout) as response:
                if response.headers.get_content_type() == "text/html": raise ProviderError("NON_API_RESPONSE")
                data = json.loads(response.read())
                if self.wire_api == "responses": return ResponsesResponseAdapter.normalize(data)
                choice = data["choices"][0]; return ProviderResponse(choice["message"]["content"], data.get("model"), choice.get("finish_reason"), data.get("usage"), "chat_completions")
        except ProviderError: raise
        except HTTPError as exc:
            if exc.code == 404 and not self._validating_candidate:
                refreshed = self.refresh_models()
                if refreshed and self.model not in {item.model_id for item in refreshed}:
                    raise ProviderError("MODEL_NOT_FOUND") from exc
            raise ProviderError("API_PROVIDER_ERROR:HTTPError") from exc
        except (URLError, TimeoutError, KeyError, json.JSONDecodeError) as exc: raise ProviderError(f"API_PROVIDER_ERROR:{type(exc).__name__}") from exc
    def ask(self, prompt: str) -> str: return self.complete(prompt).text

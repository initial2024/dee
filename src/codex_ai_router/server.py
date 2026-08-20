from __future__ import annotations

import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from . import provider_config
from .classifier import classify
from .network import NetworkMode, NetworkState
from .policy import FastLocalGate, FastLocalPolicy
from .providers import LMStudioProvider, OpenAICompatibleProvider, GroqProvider
from .providers.local_backend import LocalBackend
from .providers.openai_compatible import ResponsesResponseAdapter
from .providers.runtime_models import RuntimeModelState, text_candidates
from .vision import VisionProxy


VIRTUAL_MODELS = ("xiaoyu-auto", "xiaoyu-local", "xiaoyu-api-auto", "xiaoyu-api-local", "xiaoyu-lightboat")


def provider_virtual_model(provider_id: str) -> str: return "xiaoyu-api-" + provider_id


def _input_text(value: object) -> str:
    if isinstance(value, str):
        return value
    pieces: list[str] = []
    if isinstance(value, list):
        for item in value:
            if not isinstance(item, dict):
                continue
            content = item.get("content", [])
            if isinstance(content, str):
                pieces.append(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        pieces.append(part["text"])
    return "\n".join(pieces)


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


class RouterService:
    """Secret-free local Responses facade for a Codex custom provider."""
    def __init__(self, network_mode: NetworkMode = NetworkMode.AUTO, local: object | None = None, fast_local_policy: FastLocalPolicy | None = None, runtime_models: RuntimeModelState | None = None):
        self.network = NetworkState(network_mode)
        self.local = local or LocalBackend(lmstudio=LMStudioProvider())
        self.fast_local_policy = fast_local_policy or FastLocalPolicy()
        self.vision = VisionProxy()
        self.runtime_models = runtime_models or RuntimeModelState()

    def models(self) -> list[dict]:
        names = [*VIRTUAL_MODELS, *(provider_virtual_model(provider_id) for provider_id, _ in self._provider_entries())]
        return [{"id": name, "object": "model", "owned_by": "xiaoyu-router"} for name in names]

    def _provider_entries(self) -> list[tuple[str, dict]]:
        records = provider_config.load().get("providers", {})
        values = [(key, value) for key, value in records.items() if isinstance(value, dict) and value.get("enabled", True) and value.get("type") in {"openai_compatible", "custom_openai_compatible", "groq"}]
        return sorted(values, key=lambda item: int(item[1].get("priority", 100)))

    def _metadata_candidates(self, entry: dict) -> list[str]:
        snapshot = entry.get("model_registry", {}) if isinstance(entry.get("model_registry"), dict) else {}
        values = snapshot.get("ALLOWED_MODELS", snapshot.get("USABLE_MODELS", entry.get("last_discovery_models", [])))
        return [model for model in values if isinstance(model, str)] if isinstance(values, list) else []

    def _api_provider(self, requested: str) -> tuple[str, object, dict]:
        entries = self._provider_entries()
        if requested == "xiaoyu-lightboat":
            entries = [item for item in entries if item[0].startswith("lightboat")]
        elif requested.startswith("xiaoyu-api-") and requested != "xiaoyu-api-auto":
            provider_id = requested.removeprefix("xiaoyu-api-")
            entries = [item for item in entries if item[0] == provider_id]
        elif requested == "xiaoyu-api-auto":
            ranked: list[tuple[float, str, dict]] = []
            for provider_id, entry in entries:
                selected = self._select_text_model_for_entry(provider_id, entry)
                if selected:
                    elapsed = self.runtime_models.data.get("providers", {}).get(provider_id, {}).get(selected, {}).get("elapsed_seconds", float("inf"))
                    ranked.append((float(elapsed), provider_id, entry))
            entries = [(provider_id, entry) for _, provider_id, entry in sorted(ranked)]
        if not entries:
            raise RuntimeError("API_PROVIDER_UNAVAILABLE")
        provider_id, entry = entries[0]
        provider = GroqProvider(key_env=entry.get("api_key_env", "GROQ_API_KEY"), provider_id=provider_id, timeout=int(entry.get("request_timeout", 20))) if entry.get("type") == "groq" else OpenAICompatibleProvider(
            entry.get("base_url"), key_env=entry.get("api_key_env", ""), wire_api=entry.get("wire_api", "responses"),
            requires_bearer_auth=entry.get("requires_bearer_auth", False), header_env=entry.get("headers", {}),
            provider_id=provider_id, provider_metadata=entry, model_env=entry.get("model_env"), timeout=int(entry.get("request_timeout", 60)),
        )
        return provider_id, provider, entry

    def _select_text_model_for_entry(self, provider_id: str, entry: dict) -> str | None:
        candidates, _ = text_candidates(self._metadata_candidates(entry))
        preferred = entry.get("preferred_runtime_model")
        if isinstance(preferred, str) and preferred in candidates and not self.runtime_models.recent_timeout(provider_id, preferred):
            details = self.runtime_models.data.get("providers", {}).get(provider_id, {}).get(preferred, {})
            if details.get("status") == "PASS": return preferred
        return self.runtime_models.select(provider_id, candidates)

    def _select_text_model(self, provider: object, entry: dict) -> str:
        selected = self._select_text_model_for_entry(provider.provider_id, entry)
        if not selected and not self._metadata_candidates(entry):
            candidates, _ = text_candidates(provider.candidate_models())
            selected = self.runtime_models.select(provider.provider_id, candidates)
        if not selected:
            raise RuntimeError("DOWNSTREAM_UNAVAILABLE")
        return selected

    def _local_response(self, prompt: str, virtual_model: str) -> dict:
        if not self.local.available():
            raise RuntimeError("LOCAL_UNAVAILABLE")
        return self._response(virtual_model, self.local.ask(prompt))

    def _api_response(self, request_payload: dict, virtual_model: str) -> dict:
        if not self.network.remote_allowed:
            raise RuntimeError("REMOTE_FALLBACK_DISABLED_OFFLINE")
        _, provider, entry = self._api_provider(virtual_model)
        # AUTO makes a short reachability decision per invocation.  A later
        # invocation probes again, so recovery needs no VPN-specific logic.
        if isinstance(provider, OpenAICompatibleProvider) and self.network.mode is NetworkMode.AUTO and self.network.probe(provider.models_endpoint(), timeout=2.0) == "OFFLINE_OR_REMOTE_UNAVAILABLE":
            raise RuntimeError("REMOTE_UNAVAILABLE")
        selected = self._select_text_model(provider, entry)
        # Lightboat's minimal path accepts a plain Responses input.  Normalize
        # array-shaped Codex input into the equivalent text to avoid sending a
        # provider-specific content array downstream.
        if isinstance(provider, GroqProvider):
            try:
                provider.model = selected
                result = provider.complete(request_payload.get("input"), max_tokens=_bounded_output_tokens(request_payload) or 16)
                return self._response(virtual_model, result.text, result.usage)
            except Exception as exc:
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
        outbound["model"] = selected
        outbound["stream"] = False
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
            raise RuntimeError("DOWNSTREAM_TEXT_MISSING")
        # Return an OpenAI Responses-shaped object while never reflecting downstream headers/secrets.
        return self._response(virtual_model, text, raw.get("usage"))

    @staticmethod
    def _response(model: str, text: str, usage: dict | None = None) -> dict:
        return {"id": "resp_xiaoyu_" + uuid.uuid4().hex, "object": "response", "created_at": int(time.time()), "status": "completed", "model": model, "output": [{"id": "msg_xiaoyu_" + uuid.uuid4().hex, "type": "message", "status": "completed", "role": "assistant", "content": [{"type": "output_text", "text": text}]}], "output_text": text, "usage": usage or {}}

    def respond(self, request_payload: dict) -> dict:
        virtual_model = request_payload.get("model")
        if virtual_model not in {model["id"] for model in self.models()}:
            raise RuntimeError("UNKNOWN_VIRTUAL_MODEL")
        if _has_image(request_payload.get("input")):
            outcome = self.vision.route(True, [], self.network.mode is NetworkMode.OFFLINE)
            raise RuntimeError(outcome.status)
        prompt = _input_text(request_payload.get("input"))
        if not prompt:
            raise RuntimeError("INPUT_REQUIRED")
        if virtual_model == "xiaoyu-local":
            return self._local_response(prompt, virtual_model)
        if virtual_model == "xiaoyu-auto":
            category, risk = classify(prompt)
            if FastLocalGate(self.fast_local_policy).permits(prompt, category, risk) and self.local.available():
                return self._local_response(prompt, virtual_model)
        return self._api_response(request_payload, virtual_model)


class _Handler(BaseHTTPRequestHandler):
    server_version = "XiaoyuRouter/1.1"

    @property
    def service(self) -> RouterService:
        return self.server.service  # type: ignore[attr-defined]

    def log_message(self, _format: str, *_args: object) -> None:
        # Never include request bodies or headers in server logs.
        return

    def _send(self, status: int, body: dict) -> None:
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _send_sse(self, body: dict) -> None:
        raw = json.dumps({"type": "response.completed", "response": body}, ensure_ascii=False)
        encoded = ("event: response.completed\ndata: " + raw + "\n\ndata: [DONE]\n\n").encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:
        if self.path == "/v1/models":
            self._send(200, {"object": "list", "data": self.service.models()})
        elif self.path == "/health":
            self._send(200, {"status": "ok", "localhost_only": True})
        else:
            self._send(404, {"error": {"code": "not_found"}})

    def do_POST(self) -> None:
        if self.path != "/v1/responses":
            self._send(404, {"error": {"code": "not_found"}})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 2_000_000:
                raise RuntimeError("INVALID_REQUEST_SIZE")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise RuntimeError("INVALID_REQUEST")
            response = self.service.respond(payload)
            if payload.get("stream") is True:
                self._send_sse(response)
            else:
                self._send(200, response)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            self._send(400, {"error": {"code": "invalid_json"}})
        except RuntimeError as exc:
            code = str(exc)
            status = 504 if code.startswith("DOWNSTREAM_TIMEOUT:") else (503 if code in {"LOCAL_UNAVAILABLE", "API_PROVIDER_UNAVAILABLE", "DOWNSTREAM_UNAVAILABLE", "REMOTE_FALLBACK_DISABLED_OFFLINE", "REMOTE_UNAVAILABLE"} else 400)
            self._send(status, {"error": {"code": code}})


class RouterResponsesServer:
    """A localhost-only server. It is intentionally never bound to 0.0.0.0."""
    def __init__(self, service: RouterService | None = None, host: str = "127.0.0.1", port: int = 18789):
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("LOCALHOST_ONLY")
        self.host, self.port, self.service = host, port, service or RouterService()
        self.httpd = ThreadingHTTPServer((host, port), _Handler)
        self.httpd.service = self.service  # type: ignore[attr-defined]
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

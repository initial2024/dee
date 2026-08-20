from __future__ import annotations

import json
import os
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .base import ProviderError
from .openai_compatible import OpenAICompatibleProvider, ResponsesResponseAdapter


class RuntimeModelState:
    """Non-secret per-model probe results used to avoid repeatedly selecting slow models."""
    def __init__(self, path: Path | None = None, cooldown_seconds: int = 900):
        home = Path(os.getenv("USERPROFILE") or Path.home())
        self.path = path or home / ".codex-ai-router" / "runtime-models.json"
        self.cooldown_seconds = cooldown_seconds
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {"providers": {}}

    def _entry(self, provider_id: str, model_id: str) -> dict:
        return self.data.setdefault("providers", {}).setdefault(provider_id, {}).setdefault(model_id, {})

    def record(self, provider_id: str, model_id: str, status: str, elapsed_seconds: float, http_status: str = "") -> None:
        item = self._entry(provider_id, model_id)
        item.update({"status": status, "elapsed_seconds": round(elapsed_seconds, 3), "http_status": http_status, "checked_at": time.time()})
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        temp.replace(self.path)

    def recent_timeout(self, provider_id: str, model_id: str) -> bool:
        item = self.data.get("providers", {}).get(provider_id, {}).get(model_id, {})
        return item.get("status") == "TIMEOUT" and time.time() - float(item.get("checked_at", 0)) < self.cooldown_seconds

    def select(self, provider_id: str, candidates: list[str]) -> str | None:
        records = self.data.get("providers", {}).get(provider_id, {})
        passed = [(float(records.get(model, {}).get("elapsed_seconds", float("inf"))), model) for model in candidates if records.get(model, {}).get("status") == "PASS"]
        return min(passed)[1] if passed else None


def text_candidates(models: list[str]) -> tuple[list[str], list[str]]:
    """Only exclude explicit image-only names; all other capabilities remain unknown."""
    excluded = [model for model in models if "image" in model.lower()]
    return [model for model in models if model not in excluded], excluded


def probe_model(provider: OpenAICompatibleProvider, model: str, state: RuntimeModelState, timeout: int = 20) -> dict:
    """One bounded non-streaming Responses request; values are never logged or returned."""
    started = time.monotonic(); status = "TRANSPORT_ERROR"; content_type = "UNKNOWN"; valid_json = False; has_text = False
    payload = {"model": model, "input": "只回复：LB_OK", "max_output_tokens": 8, "stream": False}
    try:
        request = Request(provider.endpoint(), data=json.dumps(payload).encode("utf-8"), headers=provider.request_headers(content_type=True), method="POST")
        with urlopen(request, timeout=timeout) as response:
            status = "HTTP_" + str(response.status); content_type = response.headers.get_content_type()
            data = json.loads(response.read().decode("utf-8")); valid_json = True; has_text = bool(ResponsesResponseAdapter.text(data).strip())
    except HTTPError as exc:
        status = "HTTP_" + str(exc.code); content_type = exc.headers.get_content_type() if exc.headers else "UNKNOWN"
    except TimeoutError:
        status = "TIMEOUT"
    except (URLError, OSError, json.JSONDecodeError):
        status = "TRANSPORT_ERROR"
    elapsed = time.monotonic() - started
    outcome = "PASS" if status.startswith("HTTP_2") and valid_json and has_text else ("TIMEOUT" if status == "TIMEOUT" else "FAIL")
    state.record(provider.provider_id, model, outcome, elapsed, status)
    return {"model": model, "status": outcome, "http_status": status, "content_type": content_type, "valid_json": valid_json, "assistant_text": has_text, "elapsed_seconds": round(elapsed, 3)}

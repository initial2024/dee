from __future__ import annotations

import json
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .base import BaseProvider, ProviderError
from .base import DiscoveredModel


class LMStudioProvider(BaseProvider):
    name = "local"

    def __init__(self, base_url: str = "http://127.0.0.1:1234/v1", model: str = "auto", timeout: int = 60, max_tokens: int = 256):
        self.base_url, self.model, self.timeout, self.max_tokens = base_url.rstrip("/"), model, timeout, max_tokens

    def models(self) -> list[str]:
        try:
            with urlopen(self.base_url + "/models", timeout=self.timeout) as response:
                return [item["id"] for item in json.loads(response.read()).get("data", []) if item.get("id")]
        except (HTTPError, URLError, json.JSONDecodeError):
            return []

    def available(self) -> bool:
        return bool(self.models())

    def discover_models(self, refresh: bool = False) -> list[DiscoveredModel]:
        return [DiscoveredModel("local", model, model, None, None, datetime.now(timezone.utc)) for model in self.models()]

    def refresh_models(self) -> list[DiscoveredModel]:
        return self.discover_models(refresh=True)

    def candidate_models(self) -> list[str]:
        return self.models()

    def ask(self, prompt: str) -> str:
        models = self.models()
        if not models:
            raise ProviderError("LOCAL_PROVIDER_UNAVAILABLE")
        model = models[0] if self.model == "auto" else self.model
        # Bound generation so a reasoning-capable local model cannot keep a
        # simple Router request open indefinitely.  This is a normal-request
        # limit, not a blanket cold-load timeout.
        data = json.dumps({"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0, "max_tokens": self.max_tokens, "stream": False}).encode()
        request = Request(self.base_url + "/chat/completions", data=data, headers={"Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read())['choices'][0]['message']['content']
        except (HTTPError, URLError, TimeoutError, KeyError, json.JSONDecodeError) as exc:
            raise ProviderError(f"LOCAL_PROVIDER_ERROR:{type(exc).__name__}") from exc

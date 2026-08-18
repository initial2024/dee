from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .base import BaseProvider, ProviderError


class LMStudioProvider(BaseProvider):
    name = "local"

    def __init__(self, base_url: str = "http://127.0.0.1:1234/v1", model: str = "auto", timeout: int = 10):
        self.base_url, self.model, self.timeout = base_url.rstrip("/"), model, timeout

    def models(self) -> list[str]:
        try:
            with urlopen(self.base_url + "/models", timeout=self.timeout) as response:
                return [item["id"] for item in json.loads(response.read()).get("data", []) if item.get("id")]
        except (HTTPError, URLError, json.JSONDecodeError):
            return []

    def available(self) -> bool:
        return bool(self.models())

    def ask(self, prompt: str) -> str:
        models = self.models()
        if not models:
            raise ProviderError("LOCAL_PROVIDER_UNAVAILABLE")
        model = models[0] if self.model == "auto" else self.model
        data = json.dumps({"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0}).encode()
        request = Request(self.base_url + "/chat/completions", data=data, headers={"Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read())['choices'][0]['message']['content']
        except (HTTPError, URLError, KeyError, json.JSONDecodeError) as exc:
            raise ProviderError(f"LOCAL_PROVIDER_ERROR:{type(exc).__name__}") from exc

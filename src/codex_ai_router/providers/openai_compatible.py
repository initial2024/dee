from __future__ import annotations

import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .base import BaseProvider, ProviderError


class OpenAICompatibleProvider(BaseProvider):
    name = "api"

    def __init__(self, base_url: str | None = None, model: str | None = None, key_env: str = "XIAOYU_CODER_API_KEY", timeout: int = 20):
        self.base_url = (base_url or os.getenv("XIAOYU_CODER_API_BASE", "")).rstrip("/")
        self.model = model or os.getenv("XIAOYU_CODER_API_MODEL", "")
        self.key_env = os.getenv("XIAOYU_CODER_API_KEY_ENV", key_env)
        self.timeout = timeout

    def available(self) -> bool:
        return bool(self.base_url and self.model and os.getenv(self.key_env))

    def models(self) -> list[str]:
        if not self.available():
            return []
        try:
            request = Request(self.base_url + "/models", headers={"Authorization": "Bearer " + os.environ[self.key_env]})
            with urlopen(request, timeout=self.timeout) as response:
                found = [item["id"] for item in json.loads(response.read()).get("data", []) if item.get("id")]
                return found or [self.model]
        except (HTTPError, URLError, json.JSONDecodeError):
            return [self.model]

    def ask(self, prompt: str) -> str:
        if not self.available():
            raise ProviderError("API_PROVIDER_UNAVAILABLE_OR_MISSING_KEY")
        payload = json.dumps({"model": self.model, "messages": [{"role": "user", "content": prompt}], "temperature": 0}).encode()
        request = Request(self.base_url + "/chat/completions", data=payload, headers={"Authorization": "Bearer " + os.environ[self.key_env], "Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read())['choices'][0]['message']['content']
        except (HTTPError, URLError, TimeoutError, KeyError, json.JSONDecodeError) as exc:
            raise ProviderError(f"API_PROVIDER_ERROR:{type(exc).__name__}") from exc

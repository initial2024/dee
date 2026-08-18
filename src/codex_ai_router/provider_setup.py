from __future__ import annotations

import re
from urllib.parse import urlparse


KNOWN_IDS = {"lightboat": "lightboat", "groq": "groq", "openrouter": "openrouter", "nvidia": "nvidia", "deepseek": "deepseek"}


def suggested_provider_id(base_url: str, existing: set[str] = set()) -> str:
    host = (urlparse(base_url).hostname or "provider").lower()
    first = host.split(".")[0]
    fallback = "-".join(host.split(".")[:-1]) or first
    stem = next((value for key, value in KNOWN_IDS.items() if key in host), fallback)
    stem = re.sub(r"[^a-z0-9_-]+", "-", stem).strip("-_") or "provider"
    candidate, index = stem, 2
    while candidate in existing:
        candidate, index = f"{stem}-{index}", index + 1
    return candidate


def default_display_name(provider_id: str) -> str:
    """Return a readable default without changing the machine-stable ID."""
    return " ".join(part.capitalize() for part in provider_id.replace("_", "-").split("-") if part) or provider_id


def suggested_provider_type(base_url: str) -> str:
    host = (urlparse(base_url).hostname or "").lower()
    return "lmstudio" if host in {"localhost", "127.0.0.1"} and "1234" in base_url else "openai_compatible"


def classify_probe(content_type: str | None, responses_status: int | None, chat_status: int | None) -> str | None:
    if (content_type or "").lower().startswith("text/html"): return None
    if responses_status in {200, 400, 401, 403, 405}: return "responses"
    if chat_status in {200, 400, 401, 403, 405}: return "chat_completions"
    return None

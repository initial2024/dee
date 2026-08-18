from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib


def _normal_base(value: str | None) -> str:
    return (value or "").rstrip("/").removesuffix("/v1")


def _values(value: object) -> list[str]:
    if isinstance(value, str) and value.strip(): return [value.strip()]
    if isinstance(value, list): return [item.strip() for item in value if isinstance(item, str) and item.strip()]
    if isinstance(value, dict): return [item.strip() for item in value.keys() if isinstance(item, str) and item.strip()]
    return []


def codex_profile_candidates(base_url: str, path: Path | None = None) -> list[str]:
    """Read only non-sensitive model/provider fields from the Codex profile."""
    try: profile = path or Path(os.getenv("USERPROFILE") or Path.home()) / ".codex" / "config.toml"
    except RuntimeError: return []
    if not profile.exists(): return []
    try: data = tomllib.loads(profile.read_text(encoding="utf-8"))
    except (OSError, ValueError): return []
    target = _normal_base(base_url)
    candidates = _values(data.get("model"))
    providers = data.get("model_providers", {})
    if not isinstance(providers, dict): return candidates
    for entry in providers.values():
        if not isinstance(entry, dict): continue
        if target and _normal_base(entry.get("base_url")) != target: continue
        candidates.extend(_values(entry.get("model")))
    return candidates


def codex_profile_requires_bearer_auth(base_url: str, path: Path | None = None) -> bool | None:
    try: profile = path or Path(os.getenv("USERPROFILE") or Path.home()) / ".codex" / "config.toml"
    except RuntimeError: return None
    if not profile.exists(): return None
    try: data = tomllib.loads(profile.read_text(encoding="utf-8"))
    except (OSError, ValueError): return None
    target = _normal_base(base_url)
    for entry in (data.get("model_providers", {}) or {}).values():
        if isinstance(entry, dict) and _normal_base(entry.get("base_url")) == target and isinstance(entry.get("requires_openai_auth"), bool):
            return entry["requires_openai_auth"]
    return None


class ModelDiscoveryChain:
    """Collect only configured model identifiers; never infer vendor model names."""
    def __init__(self, provider_id: str, base_url: str, metadata: dict | None = None, model_env: str | None = None, profile_path: Path | None = None):
        self.provider_id, self.base_url = provider_id, base_url
        self.metadata, self.model_env, self.profile_path = metadata or {}, model_env, profile_path

    def candidates(self) -> list[tuple[str, str]]:
        values: list[tuple[str, str]] = []
        values += [(model, "EXISTING_PROVIDER_METADATA") for model in _values(self.metadata.get("models"))]
        values += [(model, "EXISTING_PROVIDER_METADATA") for model in _values(self.metadata.get("model"))]
        for env_name in filter(None, [self.model_env, self.metadata.get("model_env"), "XIAOYU_CODER_API_MODEL"]):
            if value := os.getenv(str(env_name)): values.append((value.strip(), "EXISTING_ENV_MODEL_REFERENCE"))
        values += [(model, "EXISTING_CODEX_PROFILE_METADATA") for model in codex_profile_candidates(self.base_url, self.profile_path)]
        seen: set[str] = set()
        return [(model, source) for model, source in values if model and not (model in seen or seen.add(model))]

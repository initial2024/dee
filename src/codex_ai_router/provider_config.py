from __future__ import annotations

import json, os
from pathlib import Path

VALID_ID = __import__("re").compile(r"^[a-z0-9_-]+$")
VALID_TYPES = {"openai_compatible", "custom_openai_compatible", "lmstudio"}


def config_path() -> Path:
    configured = os.getenv("XIAOYU_ROUTER_PROVIDER_CONFIG")
    return Path(configured) if configured else Path.home() / ".codex-ai-router" / "providers.json"


def load(path: Path | None = None) -> dict:
    target = path or config_path()
    if not target.exists(): return {"providers": {}}
    data = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("providers", {}), dict): raise ValueError("provider config must contain a providers object")
    data.setdefault("providers", {})
    return data


def save(data: dict, path: Path | None = None) -> None:
    target = path or config_path(); target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(target.suffix + ".tmp")
    temp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    temp.replace(target)


def validate_provider_id(provider_id: str) -> None:
    if not VALID_ID.fullmatch(provider_id): raise ValueError("provider id must match [a-z0-9_-]+")


def upsert(provider_id: str, values: dict, path: Path | None = None) -> dict:
    validate_provider_id(provider_id)
    kind = values.get("type", "openai_compatible")
    if kind not in VALID_TYPES: raise ValueError("unsupported provider type")
    data = load(path); previous = data["providers"].get(provider_id, {})
    headers = {**previous.get("headers", {}), **values.pop("headers", {})}
    data["providers"][provider_id] = {**previous, **values, "id": provider_id, "type": kind, "headers": headers}
    save(data, path); return data["providers"][provider_id]


def set_enabled(provider_id: str, enabled: bool, path: Path | None = None) -> dict:
    data = load(path)
    if provider_id not in data["providers"]: raise KeyError("provider not found")
    data["providers"][provider_id]["enabled"] = enabled; save(data, path); return data["providers"][provider_id]


def remove(provider_id: str, path: Path | None = None) -> None:
    data = load(path)
    if provider_id not in data["providers"]: raise KeyError("provider not found")
    del data["providers"][provider_id]; save(data, path)

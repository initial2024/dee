from __future__ import annotations

import json, os
from pathlib import Path

VALID_ID = __import__("re").compile(r"^[a-z0-9_-]+$")
VALID_TYPES = {"openai_compatible", "custom_openai_compatible", "lmstudio", "groq"}
LEGACY_FILE_NAME = "provider-header-mappings.json"
CANONICAL_FILE_NAME = "providers.json"


def provider_home() -> Path:
    return Path(os.getenv("USERPROFILE") or Path.home())


def config_path() -> Path:
    configured = os.getenv("XIAOYU_ROUTER_PROVIDER_CONFIG")
    return Path(configured) if configured else provider_home() / ".codex-ai-router" / CANONICAL_FILE_NAME


def legacy_config_path() -> Path:
    return provider_home() / ".codex-ai-router" / LEGACY_FILE_NAME


def _read(target: Path) -> dict:
    if not target.exists(): return {"providers": {}}
    data = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("providers", {}), dict): raise ValueError("provider config must contain a providers object")
    data.setdefault("providers", {})
    return data


def _safe_legacy_provider(provider_id: str, value: object) -> dict | None:
    if not VALID_ID.fullmatch(provider_id) or not isinstance(value, dict): return None
    if value.get("type") not in VALID_TYPES or not isinstance(value.get("base_url"), str) or not value["base_url"].strip(): return None
    allowed = {"display_name", "type", "base_url", "base_url_env", "wire_api", "enabled", "api_key_env", "headers", "model_discovery", "models", "priority", "setup_mode", "requires_bearer_auth", "inference_auth_style", "model_env", "request_timeout", "responses_token_limit_field", "model_discovery_endpoint", "model_discovery_method", "model_discovery_auth_style", "model_discovery_headers", "model_discovery_query", "model_discovery_body", "model_discovery_validate_candidates", "allowed_model_seeds", "preferred_model", "model_policy", "last_discovery_status", "last_discovery_models"}
    record = {key: value[key] for key in allowed if key in value}
    headers = record.get("headers", {})
    if not isinstance(headers, dict) or not all(isinstance(name, str) and isinstance(env_name, str) for name, env_name in headers.items()): return None
    record["id"] = provider_id
    record["display_name"] = record.get("display_name") or provider_id
    return record


def migrate_legacy_metadata(target: Path | None = None, legacy_path: Path | None = None) -> tuple[dict, list[str], list[str]]:
    """Copy valid non-secret legacy metadata once; never delete or overwrite entries."""
    canonical = target or config_path()
    legacy = legacy_path or legacy_config_path()
    data = _read(canonical)
    if not legacy.exists() or legacy.resolve() == canonical.resolve(): return data, [], []
    old = _read(legacy)
    migrated, orphaned = [], []
    for provider_id, value in old["providers"].items():
        record = _safe_legacy_provider(provider_id, value)
        if record is None:
            orphaned.append(provider_id)
        elif provider_id not in data["providers"]:
            data["providers"][provider_id] = record
            migrated.append(provider_id)
    if migrated: save(data, canonical)
    return data, migrated, orphaned


def load(path: Path | None = None) -> dict:
    target = path or config_path()
    if path is None and "XIAOYU_ROUTER_PROVIDER_CONFIG" not in os.environ:
        data, _, _ = migrate_legacy_metadata(target)
    else:
        data = _read(target)
    for provider_id, provider in data["providers"].items():
        if isinstance(provider, dict): provider.setdefault("display_name", provider_id)
    return data


def save(data: dict, path: Path | None = None) -> None:
    target = path or config_path(); target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(target.suffix + ".tmp")
    temp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    temp.replace(target)


def validate_provider_id(provider_id: str) -> None:
    if not VALID_ID.fullmatch(provider_id): raise ValueError("provider id must match [a-z0-9_-]+")


def upsert(provider_id: str, values: dict, path: Path | None = None) -> dict:
    validate_provider_id(provider_id)
    kind = values.get("type", "openai_compatible")
    if kind not in VALID_TYPES: raise ValueError("unsupported provider type")
    data = load(path); previous = data["providers"].get(provider_id, {})
    supplied = dict(values)
    headers = {**previous.get("headers", {}), **supplied.pop("headers", {})}
    display_name = supplied.get("display_name") or previous.get("display_name") or provider_id
    data["providers"][provider_id] = {**previous, **supplied, "id": provider_id, "display_name": display_name, "type": kind, "headers": headers}
    save(data, path); return data["providers"][provider_id]


def set_enabled(provider_id: str, enabled: bool, path: Path | None = None) -> dict:
    data = load(path)
    if provider_id not in data["providers"]: raise KeyError("provider not found")
    data["providers"][provider_id]["enabled"] = enabled; save(data, path); return data["providers"][provider_id]


def remove(provider_id: str, path: Path | None = None) -> None:
    data = load(path)
    if provider_id not in data["providers"]: raise KeyError("provider not found")
    del data["providers"][provider_id]; save(data, path)


def record_discovery(provider_id: str, models: list[str], status: str, path: Path | None = None) -> dict:
    """Persist only non-secret discovery metadata for control-panel readback."""
    data = load(path)
    if provider_id not in data["providers"]: raise KeyError("provider not found")
    provider = data["providers"][provider_id]
    provider["last_discovery_models"] = [model for model in models if isinstance(model, str)]
    provider["last_discovery_status"] = status
    save(data, path)
    return provider


def migrate_to_groq(provider_id: str, path: Path | None = None) -> dict:
    """Convert only non-secret metadata; preserve the provider's key-env reference."""
    data = load(path)
    if provider_id not in data["providers"]: raise KeyError("provider not found")
    provider = data["providers"][provider_id]
    provider.update({"type": "groq", "wire_api": "chat_completions", "headers": {}})
    save(data, path)
    return provider

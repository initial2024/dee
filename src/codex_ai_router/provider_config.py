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
    allowed = {"display_name", "type", "base_url", "base_url_env", "wire_api", "enabled", "api_key_env", "headers", "model_discovery", "models", "priority", "setup_mode", "requires_bearer_auth", "inference_auth_style", "model_env", "request_timeout", "responses_token_limit_field", "model_discovery_endpoint", "model_discovery_method", "model_discovery_auth_style", "model_discovery_headers", "model_discovery_query", "model_discovery_body", "model_discovery_validate_candidates", "allowed_model_seeds", "preferred_model", "preferred_runtime_model", "denied_model_ids", "model_policy", "last_discovery_status", "last_discovery_models", "model_registry"}
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
    # A transient authentication/permission failure must not erase a previously
    # successful remote catalogue.  It is still recorded as the latest status.
    if models:
        provider["last_discovery_models"] = [model for model in models if isinstance(model, str)]
    provider["last_discovery_status"] = status
    save(data, path)
    return provider


def record_model_registry(provider_id: str, states: dict, discovery_status: str, path: Path | None = None) -> dict:
    """Persist a secret-free, unified model snapshot for the CLI and control panel."""
    data = load(path)
    if provider_id not in data["providers"]: raise KeyError("provider not found")
    provider = data["providers"][provider_id]
    previous = provider.get("model_registry", {}) if isinstance(provider.get("model_registry"), dict) else {}
    previous_discovered = previous.get("DISCOVERED_MODELS", provider.get("last_discovery_models", []))
    discovered = list(states.get("DISCOVERED_MODELS", []))
    if not discovered and isinstance(previous_discovered, list): discovered = list(previous_discovered)
    runtime = list(states.get("RUNTIME_RESPONSIVE_MODELS", []))
    for model in runtime:
        if model not in discovered: discovered.append(model)
    sources = dict(previous.get("SOURCES", {})) if isinstance(previous.get("SOURCES"), dict) else {}
    for model in runtime:
        sources.setdefault(model, "RUNTIME_PROBE")
    if states.get("DISCOVERED_MODELS"):
        for model in states["DISCOVERED_MODELS"]: sources[model] = sources.get(model, "REMOTE_MODEL_LIST")
    seeds = provider.get("allowed_model_seeds", []) if isinstance(provider.get("allowed_model_seeds"), list) else []
    allowed = [model for model in discovered if (not seeds or model in seeds)]
    denied = [model for model in discovered if model not in allowed]
    usable = list(allowed)
    snapshot = {
        "DISCOVERED_MODELS": discovered,
        "USABLE_MODELS": usable,
        "ALLOWED_MODELS": allowed,
        "DENIED_MODELS": denied,
        "RUNTIME_RESPONSIVE_MODELS": runtime,
        "TIMEOUT_COOLDOWN_MODELS": list(states.get("TIMEOUT_COOLDOWN_MODELS", [])),
        "CURRENT_RUNTIME_MODEL": states.get("CURRENT_RUNTIME_MODEL"),
        "SOURCES": sources,
    }
    provider["model_registry"] = snapshot
    if discovered: provider["last_discovery_models"] = discovered
    provider["last_discovery_status"] = discovery_status
    _apply_model_policy(provider)
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


def set_runtime_model_preference(provider_id: str, model_id: str | None, path: Path | None = None) -> dict:
    data = load(path)
    if provider_id not in data["providers"]: raise KeyError("provider not found")
    provider = data["providers"][provider_id]
    states = provider.get("model_registry", {}) if isinstance(provider.get("model_registry"), dict) else {}
    usable = states.get("USABLE_MODELS", [])
    denied = set(states.get("DENIED_MODELS", [])) | set(provider.get("denied_model_ids", []))
    if model_id is not None and (model_id not in usable or model_id in denied): raise ValueError("MODEL_NOT_USABLE")
    if model_id is None: provider.pop("preferred_runtime_model", None)
    else: provider["preferred_runtime_model"] = model_id
    save(data, path)
    return provider


def set_model_denied(provider_id: str, model_id: str, denied: bool, path: Path | None = None) -> dict:
    data = load(path)
    if provider_id not in data["providers"]: raise KeyError("provider not found")
    provider = data["providers"][provider_id]
    snapshot = provider.get("model_registry", {}) if isinstance(provider.get("model_registry"), dict) else {}
    if model_id not in snapshot.get("DISCOVERED_MODELS", []): raise ValueError("MODEL_NOT_DISCOVERED")
    values = set(provider.get("denied_model_ids", []))
    if denied: values.add(model_id)
    else: values.discard(model_id)
    provider["denied_model_ids"] = sorted(values)
    _apply_model_policy(provider)
    save(data, path)
    return provider


def _apply_model_policy(provider: dict) -> None:
    snapshot = provider.setdefault("model_registry", {})
    discovered = list(snapshot.get("DISCOVERED_MODELS", provider.get("last_discovery_models", [])))
    denied = set(provider.get("denied_model_ids", []))
    allow_list = set(provider.get("allowed_model_ids", []))
    seeds = set(provider.get("allowed_model_seeds", []))
    allowed = [model for model in discovered if model not in denied and (not allow_list or model in allow_list) and (not seeds or model in seeds)]
    snapshot["DISCOVERED_MODELS"] = discovered
    snapshot["DENIED_MODELS"] = [model for model in discovered if model not in allowed]
    snapshot["ALLOWED_MODELS"] = allowed
    snapshot["USABLE_MODELS"] = list(allowed)
    snapshot["RUNTIME_RESPONSIVE_MODELS"] = [model for model in snapshot.get("RUNTIME_RESPONSIVE_MODELS", []) if model in allowed]
    if snapshot.get("CURRENT_RUNTIME_MODEL") not in allowed: snapshot["CURRENT_RUNTIME_MODEL"] = None
    if provider.get("preferred_runtime_model") not in allowed: provider.pop("preferred_runtime_model", None)


def batch_model_policy(provider_id: str, model_ids: list[str], action: str, priority: int | None = None, path: Path | None = None) -> dict:
    data = load(path)
    if provider_id not in data["providers"]: raise KeyError("provider not found")
    provider = data["providers"][provider_id]; snapshot = provider.get("model_registry", {})
    discovered = set(snapshot.get("DISCOVERED_MODELS", []))
    selected = set(model_ids)
    if not selected <= discovered: raise ValueError("MODEL_NOT_DISCOVERED")
    denied = set(provider.get("denied_model_ids", [])); allowed = set(provider.get("allowed_model_ids", [])); priorities = dict(provider.get("model_priorities", {}))
    if action == "deny": denied |= selected
    elif action == "allow": denied -= selected; allowed |= selected
    elif action == "clear_deny": denied -= selected
    elif action == "priority":
        if priority is None: raise ValueError("PRIORITY_REQUIRED")
        priorities.update({model: int(priority) for model in selected})
    elif action == "only": denied = discovered - selected; allowed = set(selected); provider["preferred_runtime_model"] = next(iter(selected)) if len(selected) == 1 else provider.get("preferred_runtime_model")
    elif action == "reset": denied.clear(); allowed.clear(); priorities.clear(); provider.pop("preferred_runtime_model", None)
    else: raise ValueError("UNKNOWN_MODEL_POLICY_ACTION")
    provider["denied_model_ids"] = sorted(denied); provider["allowed_model_ids"] = sorted(allowed); provider["model_priorities"] = priorities
    _apply_model_policy(provider); save(data, path); return provider

from __future__ import annotations

from typing import Iterable

from ..model_policy import SelectionPolicy
from .base import DiscoveredModel
from .runtime_models import RuntimeModelState
from .registry import ModelRegistry


def _unique(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    return [value for value in values if value and not (value in seen or seen.add(value))]


def model_state_report(provider_id: str, metadata: dict, records: list[DiscoveredModel], policy: SelectionPolicy | None = None, runtime: RuntimeModelState | None = None) -> dict:
    """Return non-secret model state buckets without treating discovery as permission."""
    policy = policy or SelectionPolicy()
    persisted = metadata.get("model_registry", {}) if isinstance(metadata.get("model_registry"), dict) else {}
    persisted_models = persisted.get("DISCOVERED_MODELS", metadata.get("last_discovery_models", []))
    discovered = _unique([*(record.model_id for record in records if record.availability != "NO"), *(persisted_models if isinstance(persisted_models, list) else [])])
    seeds = _unique(metadata.get("allowed_model_seeds", []) if isinstance(metadata.get("allowed_model_seeds", []), list) else [])
    candidates = _unique([*discovered, *seeds])
    provider_permitted = bool(metadata.get("enabled", True)) and policy.providers.permits(provider_id)
    usable = candidates if provider_permitted else []
    denied_metadata = set(metadata.get("denied_model_ids", [])) if isinstance(metadata.get("denied_model_ids"), list) else set()
    allowed = [model for model in usable if (not seeds or model in seeds) and model not in denied_metadata and policy.models.permits(provider_id, model)]
    denied = [model for model in candidates if model not in allowed]
    runtime = runtime or RuntimeModelState()
    runtime_records = runtime.data.get("providers", {}).get(provider_id, {})
    runtime_passed = [model for model, details in runtime_records.items() if isinstance(details, dict) and details.get("status") == "PASS"]
    for model in runtime_passed:
        if model not in discovered: discovered.append(model)
    usable = _unique([*usable, *runtime_passed]) if provider_permitted else []
    allowed = [model for model in usable if (not seeds or model in seeds) and model not in denied_metadata and policy.models.permits(provider_id, model)]
    # Runtime-probed models are usable unless an explicit deny exists.  This
    # handles a model selected before a remote list was cached.
    allowed += [model for model in runtime_passed if model not in allowed and model not in denied_metadata and policy.models.permits(provider_id, model)]
    denied = [model for model in discovered if model not in allowed]
    responsive = [model for model in allowed if model in runtime_passed]
    cooldown = [model for model in allowed if runtime.recent_timeout(provider_id, model)]
    selected = runtime.select(provider_id, responsive)
    preferred = metadata.get("preferred_runtime_model", metadata.get("preferred_model"))
    if isinstance(preferred, str) and preferred in responsive and not runtime.recent_timeout(provider_id, preferred):
        selected = preferred
    report = {
        "DISCOVERED_MODELS": discovered,
        "USABLE_MODELS": usable,
        "ALLOWED_MODELS": allowed,
        "DENIED_MODELS": denied,
        "RUNTIME_RESPONSIVE_MODELS": responsive,
        "TIMEOUT_COOLDOWN_MODELS": cooldown,
        "CURRENT_RUNTIME_MODEL": selected,
    }
    registry = ModelRegistry(); registry.update(provider_id, records); registry.set_model_states(provider_id, report)
    return registry.model_states(provider_id)

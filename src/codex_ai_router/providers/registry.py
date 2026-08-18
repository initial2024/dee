from __future__ import annotations

from .base import DiscoveredModel


class ModelRegistry:
    """In-memory unified model pool; provider discovery remains authoritative."""
    def __init__(self): self._models: dict[str, DiscoveredModel] = {}

    def update(self, provider_id: str, models: list[DiscoveredModel]) -> None:
        active = {model.qualified_id for model in models}
        for key, existing in list(self._models.items()):
            if existing.provider_id == provider_id and key not in active:
                self._models[key] = DiscoveredModel(existing.provider_id, existing.model_id, existing.display_name, existing.owned_by, existing.raw_metadata, existing.discovered_at, "NO")
        self._models.update({model.qualified_id: model for model in models})

    def all(self) -> list[DiscoveredModel]: return list(self._models.values())

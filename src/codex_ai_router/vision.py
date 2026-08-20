from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class VisionRoute:
    status: str
    provider_id: str | None = None


class VisionProxy:
    """Image input is never silently sent to a text-only downstream model."""
    def route(self, has_image: bool, eligible_vision_providers: list[str], offline: bool = False) -> VisionRoute:
        if not has_image:
            return VisionRoute("NOT_REQUIRED")
        if offline:
            return VisionRoute("VISION_PROVIDER_UNAVAILABLE")
        return VisionRoute("VISION_PROVIDER_UNAVAILABLE") if not eligible_vision_providers else VisionRoute("VISION_DESCRIPTION_REQUIRED", eligible_vision_providers[0])

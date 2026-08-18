from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any
from datetime import datetime


class ProviderError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProviderResponse:
    text: str
    model: str | None = None
    finish_reason: str | None = None
    usage: dict[str, Any] | None = None
    raw_protocol: str = "unknown"


@dataclass(frozen=True)
class DiscoveredModel:
    provider_id: str
    model_id: str
    display_name: str
    owned_by: str | None
    raw_metadata: dict[str, Any] | None
    discovered_at: datetime
    availability: str = "YES"

    @property
    def qualified_id(self) -> str:
        return f"{self.provider_id}:{self.model_id}"


class BaseProvider(ABC):
    name: str

    @abstractmethod
    def available(self) -> bool: ...

    @abstractmethod
    def models(self) -> list[str]: ...

    @abstractmethod
    def ask(self, prompt: str) -> str: ...

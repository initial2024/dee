from __future__ import annotations

from abc import ABC, abstractmethod


class ProviderError(RuntimeError):
    pass


class BaseProvider(ABC):
    name: str

    @abstractmethod
    def available(self) -> bool: ...

    @abstractmethod
    def models(self) -> list[str]: ...

    @abstractmethod
    def ask(self, prompt: str) -> str: ...

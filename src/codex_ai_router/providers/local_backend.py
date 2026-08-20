from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
import subprocess

from .base import ProviderError
from .lmstudio import LMStudioProvider


@dataclass(frozen=True)
class GGUFModel:
    path: Path
    model_id: str
    size_bytes: int
    architecture: str = "UNKNOWN"
    quantization: str = "UNKNOWN"
    context_window: str = "UNKNOWN"


class ManagedLlamaCppBackend:
    """Optional persistent llama-server backend; it never downloads models."""
    name = "llama_cpp_managed"

    def __init__(self, model_directories: list[Path] | None = None, executable: str = "llama-server", host: str = "127.0.0.1", port: int = 18790):
        self.model_directories = [Path(path) for path in (model_directories or [])]
        self.executable, self.host, self.port = executable, host, port
        self.process: subprocess.Popen | None = None
        self.selected: GGUFModel | None = None

    def discover(self) -> list[GGUFModel]:
        found: list[GGUFModel] = []
        for directory in self.model_directories:
            if not directory.is_dir():
                continue
            for path in directory.rglob("*.gguf"):
                if path.is_file():
                    found.append(GGUFModel(path.resolve(), path.stem, path.stat().st_size))
        return sorted(found, key=lambda item: item.path.name.lower())

    def executable_available(self) -> bool:
        return bool(shutil.which(self.executable))

    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def available(self) -> bool:
        return self.running()

    def start(self, model: GGUFModel) -> None:
        if not self.executable_available():
            raise ProviderError("LLAMA_CPP_UNAVAILABLE")
        if self.running():
            return
        self.selected = model
        self.process = subprocess.Popen(
            [self.executable, "-m", str(model.path), "--host", self.host, "--port", str(self.port)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def stop(self) -> None:
        if self.running():
            self.process.terminate()
        self.process = None

    def ask(self, prompt: str) -> str:
        if not self.running() or not self.selected:
            raise ProviderError("LLAMA_CPP_MANAGED_UNAVAILABLE")
        # llama-server exposes the OpenAI-compatible endpoint while it remains
        # persistent; this is not a per-request llama-cli invocation.
        return LMStudioProvider(f"http://{self.host}:{self.port}/v1", self.selected.model_id).ask(prompt)


class LocalBackend:
    """AUTO prefers a live LM Studio instance, then a managed persistent GGUF server."""
    def __init__(self, lmstudio: LMStudioProvider | None = None, managed: ManagedLlamaCppBackend | None = None):
        self.lmstudio = lmstudio or LMStudioProvider()
        self.managed = managed or ManagedLlamaCppBackend()

    def active_kind(self) -> str:
        if self.lmstudio.available():
            return "LM_STUDIO"
        if self.managed.available():
            return "LLAMA_CPP_MANAGED"
        return "LOCAL_UNAVAILABLE"

    def models(self) -> list[str]:
        if self.lmstudio.available():
            return self.lmstudio.models()
        if self.managed.available() and self.managed.selected:
            return [self.managed.selected.model_id]
        return []

    def ask(self, prompt: str) -> str:
        kind = self.active_kind()
        if kind == "LM_STUDIO":
            return self.lmstudio.ask(prompt)
        if kind == "LLAMA_CPP_MANAGED":
            return self.managed.ask(prompt)
        raise ProviderError("LOCAL_UNAVAILABLE")

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import time
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

from .base import ProviderError
from .lmstudio import LMStudioProvider


def router_user_dir() -> Path:
    return Path(os.environ.get("USERPROFILE") or Path.home()) / ".codex-ai-router"


def local_backend_config_path() -> Path:
    return router_user_dir() / "local-backend.json"


def default_lmstudio_model_dirs() -> list[Path]:
    user = Path(os.environ.get("USERPROFILE") or Path.home())
    return [user / ".lmstudio" / "models", user / ".cache" / "lm-studio" / "models"]


def default_local_backend_config() -> dict:
    return {
        "backend": "llama_cpp",
        "llama_server_path": "",
        "model_dirs": [str(path) for path in default_lmstudio_model_dirs()],
        "selected_model_path": "",
        "host": "127.0.0.1",
        "port": 18790,
        "ctx_size": 4096,
        "threads": "auto",
        "gpu_layers": "auto",
        "timeout_seconds": 60,
    }


def load_local_backend_config(path: Path | None = None) -> dict:
    target = path or local_backend_config_path()
    config = default_local_backend_config()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            config.update({key: value for key, value in raw.items() if key in config})
    except FileNotFoundError:
        pass
    except (OSError, json.JSONDecodeError) as exc:
        raise ProviderError("LOCAL_BACKEND_CONFIG_INVALID") from exc
    if not isinstance(config.get("model_dirs"), list):
        config["model_dirs"] = [str(path) for path in default_lmstudio_model_dirs()]
    config["host"] = "127.0.0.1"
    config["port"] = int(config.get("port") or 18790)
    return config


def save_local_backend_config(config: dict, path: Path | None = None) -> Path:
    target = path or local_backend_config_path()
    merged = default_local_backend_config()
    merged.update(config)
    merged["host"] = "127.0.0.1"
    merged["port"] = int(merged.get("port") or 18790)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".tmp-" + str(os.getpid()))
    temp.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(target)
    return target


def discover_llama_server(explicit: str | Path | None = None, project_root: Path | None = None) -> Path | None:
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    if explicit is None:
        config = load_local_backend_config()
        if config.get("llama_server_path"):
            candidates.append(Path(str(config["llama_server_path"])))
    root = (project_root or Path.cwd()).resolve()
    candidates.extend([root / "tools" / "llama-server.exe", root / "tools" / "llama-server", root / "llama-server.exe", root / "llama-server"])
    candidates.extend([Path(r"C:\llama.cpp\llama-server.exe"), Path(r"C:\llama.cpp\llama-server")])
    documents = Path(os.environ.get("USERPROFILE") or Path.home()) / "Documents"
    candidates.extend([documents / "llama-server.exe", documents / "llama-server"])
    for name in ("llama-server.exe", "llama-server"):
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))
    seen: set[str] = set()
    for candidate in candidates:
        key = os.path.normcase(str(candidate))
        if key in seen:
            continue
        seen.add(key)
        if candidate.is_file():
            return candidate.resolve()
    return None


def _quantization(name: str) -> str:
    match = re.search(r"(Q\d+_[A-Z0-9_]+|Q\d+_\d+|F16|F32)", name.upper())
    return match.group(1) if match else "UNKNOWN"


@dataclass(frozen=True)
class GGUFModel:
    path: Path
    model_id: str
    size_bytes: int
    architecture: str = "UNKNOWN"
    quantization: str = "UNKNOWN"
    context_window: str = "UNKNOWN"
    source: str = "configured"

    def as_dict(self) -> dict:
        return {
            "model_id": self.model_id,
            "path": str(self.path),
            "filename": self.path.name,
            "size_bytes": self.size_bytes,
            "quantization": self.quantization,
            "architecture": self.architecture,
            "context_window": self.context_window,
            "text_model": "YES",
            "source": self.source,
        }


def discover_gguf_models(model_directories: list[Path] | None = None) -> list[GGUFModel]:
    directories = model_directories if model_directories is not None else [Path(path) for path in load_local_backend_config().get("model_dirs", [])]
    defaults = {os.path.normcase(str(path.resolve())) for path in default_lmstudio_model_dirs() if path.exists()}
    found: dict[str, GGUFModel] = {}
    for directory in directories:
        directory = Path(directory).expanduser()
        if not directory.is_dir():
            continue
        for path in directory.rglob("*.gguf"):
            try:
                resolved = path.resolve()
                if not resolved.is_file():
                    continue
                source = "lmstudio" if any(os.path.normcase(str(resolved)).startswith(item) for item in defaults) else "configured"
                found[os.path.normcase(str(resolved))] = GGUFModel(resolved, resolved.stem, resolved.stat().st_size, quantization=_quantization(resolved.name), source=source)
            except OSError:
                continue
    return sorted(found.values(), key=lambda item: str(item.path).lower())


class ManagedLlamaCppBackend:
    """Direct, persistent llama-server backend; it never downloads models."""

    name = "llama_cpp_managed"

    def __init__(self, model_directories: list[Path] | None = None, executable: str = "llama-server", host: str = "127.0.0.1", port: int = 18790, config_path: Path | None = None):
        self.config_path = config_path or local_backend_config_path()
        config = load_local_backend_config(self.config_path)
        self.model_directories = [Path(path) for path in (model_directories if model_directories is not None else config.get("model_dirs", []))]
        self.executable, self.host, self.port = executable, "127.0.0.1", int(port or config.get("port", 18790))
        configured_executable = str(config.get("llama_server_path") or "")
        self.configured_executable = configured_executable
        self.ctx_size = int(config.get("ctx_size") or 4096)
        self.threads = config.get("threads", "auto")
        self.gpu_layers = config.get("gpu_layers", "auto")
        self.timeout = int(config.get("timeout_seconds") or 60)
        selected_path = str(config.get("selected_model_path") or "")
        self.process: subprocess.Popen | None = None
        self.selected: GGUFModel | None = None
        if selected_path and Path(selected_path).is_file():
            selected_file = Path(selected_path).resolve()
            self.selected = GGUFModel(selected_file, selected_file.stem, selected_file.stat().st_size, quantization=_quantization(selected_file.name), source="lmstudio" if "lmstudio" in str(selected_file).lower() else "configured")

    @property
    def pid_path(self) -> Path:
        return router_user_dir() / "local-backend.pid"

    @property
    def state_path(self) -> Path:
        return router_user_dir() / "local-backend.state.json"

    def discover(self) -> list[GGUFModel]:
        return discover_gguf_models(self.model_directories)

    def executable_path(self) -> Path | None:
        return discover_llama_server(self.configured_executable or self.executable)

    def executable_available(self) -> bool:
        return self.executable_path() is not None

    def _external_pid(self) -> int | None:
        try:
            return int(self.pid_path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None

    @staticmethod
    def _pid_alive(pid: int | None) -> bool:
        if not pid:
            return False
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False

    def running(self) -> bool:
        if self.process is not None and self.process.poll() is None:
            return True
        return self._pid_alive(self._external_pid())

    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def status(self) -> dict:
        executable = self.executable_path()
        return {
            "backend": "llama.cpp direct",
            "LMSTUDIO_SERVER_REQUIRED": "NO",
            "LMSTUDIO_GGUF_REUSE": "YES",
            "configured": "YES" if self.config_path.exists() else "NO",
            "llama_server_found": "YES" if executable else "NO",
            "LLAMA_SERVER_NOT_FOUND_CLEAR": "YES",
            "NO_FULL_DISK_SCAN": "YES",
            "llama_server_path": str(executable) if executable else None,
            "selected_model": self.selected.as_dict() if self.selected else None,
            "server_running": "YES" if self.running() else "NO",
            "endpoint": self.endpoint(),
            "model_count": len(self.discover()),
        }

    def _persist_selected(self, model: GGUFModel) -> None:
        config = load_local_backend_config(self.config_path)
        config["selected_model_path"] = str(model.path)
        config["llama_server_path"] = str(self.executable_path() or config.get("llama_server_path") or "")
        save_local_backend_config(config, self.config_path)
        self.selected = model

    def select(self, model: str | Path) -> GGUFModel:
        candidates = self.discover()
        target = Path(model).expanduser()
        chosen = next((item for item in candidates if item.model_id == str(model) or item.path == target or str(item.path) == str(model)), None)
        if chosen is None:
            raise ProviderError("MODEL_NOT_FOUND")
        self._persist_selected(chosen)
        return chosen

    def _command(self, model: GGUFModel) -> list[str]:
        executable = self.executable_path()
        if executable is None:
            raise ProviderError("LLAMA_SERVER_NOT_FOUND")
        command = [str(executable), "-m", str(model.path), "--host", "127.0.0.1", "--port", str(self.port), "--ctx-size", str(self.ctx_size)]
        if isinstance(self.threads, int) or (isinstance(self.threads, str) and self.threads.isdigit()):
            command.extend(["--threads", str(self.threads)])
        if isinstance(self.gpu_layers, int) or (isinstance(self.gpu_layers, str) and self.gpu_layers.isdigit()):
            command.extend(["--n-gpu-layers", str(self.gpu_layers)])
        return command

    def wait_ready(self, timeout: float | None = None) -> bool:
        deadline = time.monotonic() + (timeout if timeout is not None else self.timeout)
        while time.monotonic() < deadline:
            if not self.running():
                return False
            try:
                with urlopen(self.endpoint() + "/models", timeout=2) as response:
                    if response.status == 200:
                        return True
            except (HTTPError, URLError, TimeoutError, OSError):
                time.sleep(0.2)
        return False

    def port_in_use(self) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", self.port), timeout=0.2):
                return True
        except OSError:
            return False

    def start(self, model: GGUFModel | None = None) -> dict:
        if self.running():
            return self.status()
        if self.port_in_use():
            raise ProviderError("PORT_IN_USE")
        if not self.executable_available():
            raise ProviderError("LLAMA_SERVER_NOT_FOUND")
        selected = model or self.selected
        if selected is None:
            discovered = self.discover()
            if not discovered:
                raise ProviderError("NO_GGUF_MODEL")
            raise ProviderError("MODEL_NOT_SELECTED")
        if not selected.path.is_file():
            raise ProviderError("NO_GGUF_MODEL")
        command = self._command(selected)
        router_user_dir().mkdir(parents=True, exist_ok=True)
        try:
            self.process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.pid_path.write_text(str(self.process.pid), encoding="utf-8")
            self.state_path.write_text(json.dumps({"selected_model_path": str(selected.path), "endpoint": self.endpoint()}, ensure_ascii=False), encoding="utf-8")
        except OSError as exc:
            raise ProviderError("START_FAILED") from exc
        self.selected = selected
        if not self.wait_ready():
            self.stop()
            raise ProviderError("HEALTH_TIMEOUT")
        return self.status()

    def stop(self) -> dict:
        pid = self.process.pid if self.process is not None else self._external_pid()
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
        elif pid and self._pid_alive(pid):
            try:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                else:
                    os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        self.process = None
        for path in (self.pid_path, self.state_path):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        return self.status()

    def restart(self, model: GGUFModel | None = None) -> dict:
        self.stop()
        return self.start(model)

    def available(self) -> bool:
        return self.running() or (self.selected is not None and self.executable_available())

    def ask(self, prompt: str) -> str:
        if self.selected is None:
            if not self.executable_available():
                raise ProviderError("LLAMA_SERVER_NOT_FOUND")
            raise ProviderError("MODEL_NOT_SELECTED")
        if not self.running():
            self.start(self.selected)
        provider = LMStudioProvider(self.endpoint(), self.selected.model_id, timeout=self.timeout, max_tokens=64)
        try:
            return provider.ask(prompt)
        except ProviderError as exc:
            if "TIMEOUT" in str(exc).upper():
                raise ProviderError("COMPLETION_TIMEOUT") from exc
            raise ProviderError("BAD_RESPONSE") from exc


class LocalBackend:
    """Direct GGUF backend is primary; LM Studio server is an optional fallback."""

    def __init__(self, lmstudio: LMStudioProvider | None = None, managed: ManagedLlamaCppBackend | None = None):
        self.lmstudio = lmstudio or LMStudioProvider()
        self.managed = managed or ManagedLlamaCppBackend()

    @property
    def model(self) -> str:
        if self.managed.selected:
            return self.managed.selected.model_id
        return getattr(self.lmstudio, "model", "auto")

    @model.setter
    def model(self, value: str) -> None:
        if value and value != "auto":
            try:
                self.managed.select(value)
                return
            except ProviderError:
                pass
        self.lmstudio.model = value

    def active_kind(self) -> str:
        if self.managed.running():
            return "LLAMA_CPP_DIRECT"
        if self.managed.selected and self.managed.executable_available():
            return "LLAMA_CPP_CONFIGURED"
        if self.lmstudio.available():
            return "LM_STUDIO_FALLBACK"
        return "LOCAL_UNAVAILABLE"

    def available(self) -> bool:
        return self.managed.available() or self.lmstudio.available()

    def models(self) -> list[str]:
        direct = self.managed.discover()
        if direct and self.managed.executable_available():
            return [item.model_id for item in direct]
        if self.lmstudio.available():
            return self.lmstudio.models()
        return [item.model_id for item in direct]

    def candidate_models(self) -> list[str]:
        return self.models()

    def ask(self, prompt: str) -> str:
        if self.managed.executable_available() and self.managed.selected:
            return self.managed.ask(prompt)
        if self.lmstudio.available():
            return self.lmstudio.ask(prompt)
        if self.managed.discover():
            return self.managed.ask(prompt)
        raise ProviderError("LOCAL_DIRECT_BACKEND_NOT_CONFIGURED")

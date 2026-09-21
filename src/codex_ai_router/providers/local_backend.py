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
from .local_model_selector import LocalModelSelector, bonsai_format_from_filename, profiles_for_models
from .local_profiles import local_profile_summaries


LOCAL_PORT_FALLBACKS = (18791, 18792, 18793, 18794, 18795)
STANDARD_RUNTIME_ID = "standard_llama_cpp"
PRISM_RUNTIME_ID = "prism_bonsai"
OFFICIAL_PRISMML_SOURCES = {
    "https://github.com/PrismML-Eng/Bonsai-demo",
    "https://github.com/PrismML-Eng/llama.cpp",
}
LOCAL_PERFORMANCE_PROFILES = {
    "fast": {"ctx_size": 2048, "max_tokens": 64, "prefer_small_models": True, "low_latency": True},
    "balanced": {"ctx_size": 4096, "max_tokens": 128, "prefer_small_models": False, "low_latency": False},
    "quality": {"ctx_size": 4096, "max_tokens": 256, "prefer_small_models": False, "low_latency": False, "explicit_only": True},
}
BONSAI_MINIMAL_SMOKE = {
    "prompt": "只回复 OK",
    "max_tokens": 4,
    "temperature": 0,
    "ctx_size": 1024,
    "timeout_seconds": 300,
}


def classify_bonsai_performance(status: str, elapsed_seconds: float | None, error_code: str | None = None) -> tuple[str, str]:
    """Classify a bounded Bonsai completion without making it auto-selectable."""
    elapsed = float(elapsed_seconds or 0)
    normalized = str(status).upper()
    code = str(error_code or "").upper()
    if normalized == "PASS":
        if elapsed < 60:
            return "BONSAI_USABLE_FAST", "Manual prefer-bonsai for complex analysis, code review, or historical reasoning."
        if elapsed <= 300:
            return "BONSAI_USABLE_SLOW", "Manual prefer-bonsai only; do not use for simple tasks or ordinary delegation."
    if "TIMEOUT" in code or normalized == "TIMEOUT":
        return "BONSAI_LOADS_BUT_TOO_SLOW", "Installed but too slow for this machine; keep out of automatic selection."
    return "BONSAI_RUNTIME_COMPLETION_BROKEN", "Keep out of automatic selection until the completion path is repaired."


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
        "auto_select_model": True,
        "manual_disabled_models": [],
        "manual_preferred_model": "",
        "manual_only_model": "",
        "allow_slow_local": False,
        "allow_bf16_auto": False,
        "active_performance_profile": "balanced",
        "performance_profiles": LOCAL_PERFORMANCE_PROFILES,
        "model_profiles": {},
        "runtimes": {
            STANDARD_RUNTIME_ID: {"runtime_id": STANDARD_RUNTIME_ID, "llama_server_path": ""},
            PRISM_RUNTIME_ID: {
                "runtime_id": PRISM_RUNTIME_ID,
                "runtime_path": str(Path.cwd() / "tools" / "prism-bonsai-runtime"),
                "llama_server_path": "",
                "official_source": "https://github.com/PrismML-Eng/Bonsai-demo",
            },
        },
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
    if not isinstance(config.get("runtimes"), dict):
        config["runtimes"] = default_local_backend_config()["runtimes"]
    standard = config["runtimes"].setdefault(STANDARD_RUNTIME_ID, {"runtime_id": STANDARD_RUNTIME_ID, "llama_server_path": ""})
    if not isinstance(standard, dict):
        standard = config["runtimes"][STANDARD_RUNTIME_ID] = {"runtime_id": STANDARD_RUNTIME_ID, "llama_server_path": ""}
    # Keep the pre-runtime-registry config key authoritative for existing installs.
    standard["llama_server_path"] = str(config.get("llama_server_path") or standard.get("llama_server_path") or "")
    prism = config["runtimes"].setdefault(PRISM_RUNTIME_ID, default_local_backend_config()["runtimes"][PRISM_RUNTIME_ID])
    if not isinstance(prism, dict):
        config["runtimes"][PRISM_RUNTIME_ID] = default_local_backend_config()["runtimes"][PRISM_RUNTIME_ID]
    config["host"] = "127.0.0.1"
    config["port"] = int(config.get("port") or 18790)
    return config


def _runtime_server_path(runtime: dict) -> Path | None:
    explicit = str(runtime.get("llama_server_path") or "")
    if explicit and Path(explicit).is_file():
        return Path(explicit).resolve()
    root = Path(str(runtime.get("runtime_path") or "")).expanduser()
    for name in ("llama-server.exe", "llama-server", "bin/llama-server.exe", "bin/llama-server"):
        candidate = root / name
        if candidate.is_file():
            return candidate.resolve()
    return None


def _runtime_probe_output(executable: Path | None) -> tuple[str, str]:
    if executable is None:
        return "", ""
    try:
        version = subprocess.run([str(executable), "--version"], capture_output=True, text=True, timeout=5, check=False)
        help_result = subprocess.run([str(executable), "--help"], capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return "", ""
    # The bounded summary is diagnostics only; it never includes environment data.
    return (version.stdout + version.stderr)[:1200], (help_result.stdout + help_result.stderr)[:4000]


def _marker(runtime_path: Path) -> dict:
    marker = runtime_path / "prism-bonsai-runtime.json"
    try:
        raw = json.loads(marker.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def prism_bonsai_runtime_status(config: dict | None = None) -> dict:
    """Probe only the dedicated Bonsai runtime; never download or build."""
    config = config or load_local_backend_config()
    runtime = dict((config.get("runtimes") or {}).get(PRISM_RUNTIME_ID) or {})
    runtime_path = Path(str(runtime.get("runtime_path") or Path.cwd() / "tools" / "prism-bonsai-runtime"))
    executable = _runtime_server_path(runtime)
    version, help_text = _runtime_probe_output(executable)
    marker = _marker(runtime_path)
    marker_source = str(marker.get("official_source") or "")
    official_marker = marker_source in OFFICIAL_PRISMML_SOURCES and str(marker.get("runtime_id") or "") == PRISM_RUNTIME_ID
    combined = (version + "\n" + help_text).lower()
    marker_formats = {str(item).upper() for item in marker.get("supported_formats", []) if isinstance(item, str)}
    supports = {
        "pq2_0": "PQ2_0" in marker_formats or "pq2_0" in combined,
        "ptq1_0": "PTQ1_0" in marker_formats or "ptq1_0" in combined,
        "q2_0_g64": "Q2_0_G64" in marker_formats or "q2_0_g64" in combined or "q2-g64" in combined,
    }
    installed = runtime_path.is_dir() and executable is not None
    supported = official_marker or all(supports.values())
    compatibility = "PASS" if installed and supported else "BONSAI_RUNTIME_UNKNOWN"
    return {
        "runtime_id": PRISM_RUNTIME_ID,
        "runtime_installed": "YES" if installed else "NO",
        "runtime_path": str(runtime_path),
        "llama_server_path": str(executable) if executable else None,
        "version_summary": version.splitlines()[:4],
        "help_summary": help_text.splitlines()[:8],
        "supports_pq2_0": "YES" if supports["pq2_0"] else "NO",
        "supports_ptq1_0": "YES" if supports["ptq1_0"] else "NO",
        "supports_q2_0_g64": "YES" if supports["q2_0_g64"] else "NO",
        "official_prismml_marker": "YES" if official_marker else "NO",
        "compatibility_status": compatibility,
        "BONSAI_RUNTIME_UNKNOWN": "YES" if compatibility != "PASS" else "NO",
        "NO_IMPLICIT_RUNTIME_BUILD": "YES",
        "OFFICIAL_PRISMML_RUNTIME_ONLY": "YES",
    }


def runtime_for_model(model: "GGUFModel", config: dict | None = None) -> tuple[str | None, str]:
    """Return the required runtime id without treating a filename as compatibility proof."""
    filename = model.path.name
    quantization = str(model.quantization or "UNKNOWN").upper()
    if not model.text_model:
        return None, "MMPROJ_NOT_TEXT_MODEL"
    bonsai_format = bonsai_format_from_filename(filename)
    bonsai = bonsai_format != "UNKNOWN" or bool(re.search(r"(?:^|[-_.])(?:ternary[-_.])?bonsai(?:[-_.]|$)", filename, re.I))
    if bonsai and bonsai_format in {"PQ2_0", "PTQ1_0", "Q1_0"}:
        return PRISM_RUNTIME_ID, "BONSAI_PRISM_RUNTIME_REQUIRED"
    if bonsai and bonsai_format == "Q2_0_G64":
        standard = config or load_local_backend_config()
        standard_path = str(((standard.get("runtimes") or {}).get(STANDARD_RUNTIME_ID) or {}).get("llama_server_path") or standard.get("llama_server_path") or "")
        _, standard_help = _runtime_probe_output(Path(standard_path) if standard_path else None)
        if "q2_0_g64" in standard_help.lower() or "q2-g64" in standard_help.lower():
            return STANDARD_RUNTIME_ID, "STANDARD_RUNTIME_EXPLICIT_Q2_0_G64"
        return PRISM_RUNTIME_ID, "BONSAI_PRISM_RUNTIME_REQUIRED"
    return STANDARD_RUNTIME_ID, "STANDARD_GGUF_RUNTIME"


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
    bonsai_format = bonsai_format_from_filename(name)
    if bonsai_format != "UNKNOWN":
        return bonsai_format
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
    vision_projector: bool = False
    text_model: bool = True

    def as_dict(self) -> dict:
        return {
            "model_id": self.model_id,
            "path": str(self.path),
            "filename": self.path.name,
            "size_bytes": self.size_bytes,
            "quantization": self.quantization,
            "architecture": self.architecture,
            "context_window": self.context_window,
            "text_model": "YES" if self.text_model else "NO",
            "vision_projector": "YES" if self.vision_projector else "NO",
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
                projector = "mmproj" in resolved.name.lower() or "vision-projector" in resolved.name.lower()
                found[os.path.normcase(str(resolved))] = GGUFModel(resolved, resolved.stem, resolved.stat().st_size, quantization=_quantization(resolved.name), source=source, vision_projector=projector, text_model=not projector)
            except OSError:
                continue
    return sorted(found.values(), key=lambda item: str(item.path).lower())


class ManagedLlamaCppBackend:
    """Direct, persistent llama-server backend; it never downloads models."""

    name = "llama_cpp_managed"

    def __init__(self, model_directories: list[Path] | None = None, executable: str = "llama-server", host: str = "127.0.0.1", port: int = 18790, config_path: Path | None = None):
        self._uses_default_state = config_path is None
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
        self._last_port_event: dict = {}
        if selected_path and Path(selected_path).is_file():
            selected_file = Path(selected_path).resolve()
            projector = "mmproj" in selected_file.name.lower() or "vision-projector" in selected_file.name.lower()
            self.selected = GGUFModel(selected_file, selected_file.stem, selected_file.stat().st_size, quantization=_quantization(selected_file.name), source="lmstudio" if "lmstudio" in str(selected_file).lower() else "configured", vision_projector=projector, text_model=not projector)

    @property
    def pid_path(self) -> Path:
        return router_user_dir() / "local-backend.pid" if self._uses_default_state else self.config_path.with_name("local-backend.pid")

    @property
    def state_path(self) -> Path:
        return router_user_dir() / "local-backend.state.json" if self._uses_default_state else self.config_path.with_name("local-backend.state.json")

    def discover(self) -> list[GGUFModel]:
        return discover_gguf_models(self.model_directories)

    def _profile_config(self) -> dict:
        config = load_local_backend_config(self.config_path)
        raw = config.get("model_profiles")
        return raw if isinstance(raw, dict) else {}

    def profiles(self) -> list[dict]:
        """Discover and persist non-secret model profiles."""
        models = self.discover()
        config = load_local_backend_config(self.config_path)
        persisted = self._profile_config()
        disabled = set(str(item) for item in config.get("manual_disabled_models", []) if item)
        preferred = str(config.get("manual_preferred_model") or "")
        only = str(config.get("manual_only_model") or "")
        result = profiles_for_models(models, persisted)
        changed = False
        for item in result:
            model_id = str(item.get("model_id"))
            item["manual_disabled"] = model_id in disabled
            item["manual_preferred"] = bool(preferred and model_id == preferred)
            item["manual_only"] = bool(only and model_id == only)
            if persisted.get(model_id) != item:
                persisted[model_id] = item
                changed = True
        if changed:
            config["model_profiles"] = persisted
            save_local_backend_config(config, self.config_path)
        return result

    def explain_select(self, task: str, risk: str = "auto", mode: str = "auto") -> dict:
        config = load_local_backend_config(self.config_path)
        profiles = self.profiles()
        policy = {
            "manual_disabled_models": config.get("manual_disabled_models", []),
            "manual_preferred_model": config.get("manual_preferred_model", ""),
            "manual_only_model": config.get("manual_only_model", ""),
        }
        selector = LocalModelSelector(profiles, self.selected.model_id if self.selected else None, policy)
        return selector.select(task, risk=risk, mode=mode, allow_slow_local=bool(config.get("allow_slow_local")), allow_bf16_auto=bool(config.get("allow_bf16_auto")))

    def auto_select(self, task: str, risk: str = "auto", mode: str = "auto", apply: bool = True) -> dict:
        selection = self.explain_select(task, risk=risk, mode=mode)
        selected_id = selection.get("selected_model")
        if selected_id and apply:
            chosen = next((item for item in self.discover() if item.model_id == selected_id), None)
            if chosen is None:
                selection["selected_model"] = None
                selection["error_code"] = "MODEL_NOT_FOUND"
                selection["requires_api_or_official_codex"] = True
            elif not self.selected or self.selected.model_id != chosen.model_id:
                self._persist_selected(chosen)
        return selection

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
    def _process_info(pid: int) -> dict:
        info = {"pid": pid, "process_name": None, "executable_path": None, "command_line": None}
        if os.name == "nt":
            try:
                result = subprocess.run(["powershell.exe", "-NoProfile", "-Command", f"$p=Get-CimInstance Win32_Process -Filter 'ProcessId={pid}'; if($p){{[pscustomobject]@{{Name=$p.Name;Path=$p.ExecutablePath;CommandLine=$p.CommandLine}}|ConvertTo-Json -Compress}}"], capture_output=True, text=True, timeout=3, check=False)
                if result.stdout.strip():
                    raw = json.loads(result.stdout)
                    info.update({"process_name": raw.get("Name"), "executable_path": raw.get("Path"), "command_line": raw.get("CommandLine")})
            except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
                pass
        else:
            try:
                info["process_name"] = subprocess.run(["ps", "-p", str(pid), "-o", "comm="], capture_output=True, text=True, timeout=2, check=False).stdout.strip() or None
                exe = Path(f"/proc/{pid}/exe")
                info["executable_path"] = str(exe.resolve()) if exe.exists() else None
                cmdline = Path(f"/proc/{pid}/cmdline")
                if cmdline.exists():
                    info["command_line"] = cmdline.read_bytes().replace(b"\x00", b" ").decode("utf-8", "replace").strip()
            except (OSError, subprocess.SubprocessError):
                pass
        return info

    def port_owner(self, port: int | None = None) -> dict | None:
        target_port = int(port or self.port)
        pids: list[int] = []
        if os.name == "nt":
            try:
                output = subprocess.run(["netstat", "-ano", "-p", "tcp"], capture_output=True, text=True, timeout=3, check=False).stdout
                for line in output.splitlines():
                    fields = line.split()
                    if len(fields) >= 5 and fields[0].upper() == "TCP" and fields[3].upper() == "LISTENING":
                        local = fields[1].rsplit(":", 1)
                        if len(local) == 2 and local[1] == str(target_port):
                            try: pids.append(int(fields[4]))
                            except ValueError: pass
            except (OSError, subprocess.SubprocessError):
                pass
        else:
            try:
                output = subprocess.run(["ss", "-ltnp"], capture_output=True, text=True, timeout=3, check=False).stdout
                for line in output.splitlines():
                    if f":{target_port} " in line or f":{target_port}\n" in line:
                        match = re.search(r"pid=(\d+)", line)
                        if match: pids.append(int(match.group(1)))
            except (OSError, subprocess.SubprocessError):
                pass
        if not pids and self.port_in_use(target_port):
            return {"pid": None, "process_name": "UNKNOWN", "executable_path": None, "command_line": None, "port": target_port}
        if not pids:
            return None
        info = self._process_info(pids[0]); info["port"] = target_port
        return info

    def _owner_matches_backend(self, owner: dict | None) -> bool:
        if not owner:
            return False
        pid = owner.get("pid")
        if pid and self.process is not None and pid == self.process.pid and self.process.poll() is None:
            return True
        name = str(owner.get("process_name") or "").lower()
        if Path(name).name not in {"llama-server", "llama-server.exe"}:
            return False
        configured = self.executable_path()
        executable = str(owner.get("executable_path") or "")
        if configured and executable and os.path.normcase(os.path.abspath(executable)) == os.path.normcase(os.path.abspath(str(configured))):
            return True
        selected = str(self.selected.path) if self.selected else ""
        return bool(selected and selected.lower() in str(owner.get("command_line") or "").lower())

    def _terminate_owner(self, owner: dict) -> bool:
        pid = owner.get("pid")
        if not pid or not self._owner_matches_backend(owner):
            return False
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, text=True, timeout=5, check=False)
            else:
                os.kill(pid, signal.SIGTERM)
            return True
        except (OSError, subprocess.SubprocessError):
            return False

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
        pid = self._external_pid()
        owner = self.port_owner(self.port)
        if owner and self._owner_matches_backend(owner):
            return True
        if not self._pid_alive(pid):
            self._clear_state_files()
            return False
        return bool(owner and owner.get("pid") == pid and self._owner_matches_backend(owner))

    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def _clear_state_files(self) -> None:
        for path in (self.pid_path, self.state_path):
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    def _wait_port_free(self, port: int | None = None, timeout: float = 5.0) -> bool:
        target = int(port or self.port)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.port_in_use(target):
                return True
            time.sleep(0.1)
        return not self.port_in_use(target)

    def set_port(self, port: int, persist: bool = True) -> int:
        selected = int(port)
        if selected < 1024 or selected > 65535:
            raise ProviderError("INVALID_PORT")
        self.port = selected
        if persist:
            config = load_local_backend_config(self.config_path)
            config["port"] = selected
            save_local_backend_config(config, self.config_path)
        return selected

    def _reconcile_state(self) -> dict:
        pid = self._external_pid()
        if pid and not self._pid_alive(pid):
            self._clear_state_files()
            return {"state_reconciled": "STALE_PID_CLEARED", "managed_pid": None}
        owner = self.port_owner(self.port)
        if owner and self._owner_matches_backend(owner):
            if pid and owner.get("pid") == pid:
                return {"state_reconciled": "MANAGED_PROCESS_MATCH", "managed_pid": pid}
            return {"state_reconciled": "EXTERNAL_MANAGED_PROCESS_DETECTED", "managed_pid": owner.get("pid")}
        if pid and owner and owner.get("pid") == pid:
            return {"state_reconciled": "PID_OWNER_MISMATCH", "managed_pid": pid}
        return {"state_reconciled": "YES", "managed_pid": pid}

    def _prepare_port(self) -> dict:
        current = self.port
        stale_pid = self._external_pid()
        if stale_pid and self._pid_alive(stale_pid) and self.process is None:
            stale_owner = self._process_info(stale_pid)
            if self._owner_matches_backend(stale_owner):
                if self._terminate_owner({**stale_owner, "port": current}):
                    self._clear_state_files()
                    self._wait_port_free(current)
        owner = self.port_owner(current)
        if owner is None:
            self._last_port_event = {"port": current, "auto_port_fallback": "NO"}
            return dict(self._last_port_event)
        if self._owner_matches_backend(owner):
            terminated = self._terminate_owner(owner)
            if terminated and self._wait_port_free(current):
                self._clear_state_files()
                self._last_port_event = {"port": current, "auto_port_fallback": "NO", "stale_llama_server_cleaned": "YES"}
                return dict(self._last_port_event)
            raise ProviderError("PORT_IN_USE_BY_MANAGED_PROCESS")
        for candidate in LOCAL_PORT_FALLBACKS:
            if candidate == current:
                continue
            if self.port_owner(candidate) is None:
                self.set_port(candidate)
                self._last_port_event = {"port": candidate, "auto_port_fallback": "YES", "port_fallback_from": current}
                return dict(self._last_port_event)
        raise ProviderError("PORT_IN_USE_BY_UNKNOWN_PROCESS")

    def status(self) -> dict:
        executable = self.executable_path()
        reconciliation = self._reconcile_state()
        owner = self.port_owner(self.port)
        return {
            "backend": "llama.cpp direct",
            "DIRECT_LOCAL_MODEL_STUDIO": "YES",
            "LOCAL_OPENAI_COMPATIBLE_ENDPOINT": "YES",
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
            "port": self.port,
            "port_in_use": "YES" if owner else "NO",
            "port_owner": owner,
            "auto_port_fallback": self._last_port_event.get("auto_port_fallback", "NO"),
            "port_fallback_from": self._last_port_event.get("port_fallback_from"),
            "runtime_registry": self.runtime_registry(),
            "performance_profiles": self.performance_status(),
            "bonsai_support": self.bonsai_support_status(),
            "history_profiles": local_profile_summaries(),
            **reconciliation,
        }

    def performance_status(self) -> dict:
        config = load_local_backend_config(self.config_path)
        active = str(config.get("active_performance_profile") or "balanced")
        profiles = config.get("performance_profiles") if isinstance(config.get("performance_profiles"), dict) else LOCAL_PERFORMANCE_PROFILES
        return {
            "LOCAL_PERFORMANCE_PROFILES": "YES",
            "LOCAL_FAST_PROFILE_IMPLEMENTED": "YES",
            "active_profile": active,
            "profiles": profiles,
            "configured_ctx_size": self.ctx_size,
            "configured_threads": self.threads,
            "configured_gpu_layers": self.gpu_layers,
            "KEEP_WARM_MODEL_WHEN_REASONABLE": "YES",
            "MODEL_SWITCH_COST_ACCOUNTED": "YES",
        }

    def set_performance_profile(self, profile_id: str) -> dict:
        if profile_id not in LOCAL_PERFORMANCE_PROFILES:
            raise ProviderError("LOCAL_PERFORMANCE_PROFILE_NOT_FOUND")
        config = load_local_backend_config(self.config_path)
        settings = dict(LOCAL_PERFORMANCE_PROFILES[profile_id])
        config["active_performance_profile"] = profile_id
        config["ctx_size"] = settings["ctx_size"]
        save_local_backend_config(config, self.config_path)
        self.ctx_size = settings["ctx_size"]
        return {"status": "PERFORMANCE_PROFILE_SAVED", "active_profile": profile_id, "settings": settings, "restart_required": "YES" if self.running() else "NO"}

    def record_model_performance(self, model_id: str, generation_tps: float, prompt_tps: float, cold_start_seconds: float | None = None, switch_cost_seconds: float | None = None) -> dict:
        config = load_local_backend_config(self.config_path)
        profiles = config.get("model_profiles") if isinstance(config.get("model_profiles"), dict) else {}
        profile = dict(profiles.get(model_id) or {})
        measured_tps = round(float(generation_tps), 3)
        speed_class = "SLOW" if measured_tps < 2 else ("FAST" if measured_tps >= 8 else "BALANCED")
        profile.update({"measured_generation_tps": measured_tps, "measured_prompt_tps": round(float(prompt_tps), 3), "cold_start_seconds": cold_start_seconds, "model_switch_cost_seconds": switch_cost_seconds, "measured_speed_class": speed_class})
        if str(profile.get("bonsai_model", "NO")).upper() == "YES" and measured_tps < 2:
            profile.update({
                "bonsai_performance_class": "BONSAI_USABLE_SLOW",
                "bonsai_recommended_use": "Manual prefer-bonsai only; measured generation is below the fast threshold.",
                "bonsai_auto_select_allowed": "NO",
            })
        profiles[model_id] = profile
        config["model_profiles"] = profiles
        save_local_backend_config(config, self.config_path)
        return profile

    def runtime_registry(self) -> dict:
        """Expose the separate standard and Prism runtime slots without changing either."""
        config = load_local_backend_config(self.config_path)
        standard = dict((config.get("runtimes") or {}).get(STANDARD_RUNTIME_ID) or {})
        executable = self.executable_path()
        standard["runtime_id"] = STANDARD_RUNTIME_ID
        standard["llama_server_path"] = str(executable) if executable else None
        standard["runtime_installed"] = "YES" if executable else "NO"
        prism = prism_bonsai_runtime_status(config)
        return {
            "LOCAL_RUNTIME_REGISTRY": "YES",
            "STANDARD_LLAMA_CPP_PRESERVED": "YES",
            "PRISM_BONSAI_RUNTIME_SLOT": "YES",
            STANDARD_RUNTIME_ID: standard,
            PRISM_RUNTIME_ID: prism,
        }

    def serve(self) -> dict:
        """Start a selected model, or safely select one, on loopback only."""
        selection = None
        if self.selected is None:
            selection = self.auto_select("解释本地模型服务状态", risk="simple", mode="local", apply=True)
            if not selection.get("selected_model"):
                return {
                    "status": "ERROR",
                    "action": "serve",
                    "error_code": selection.get("error_code") or "LOCAL_NO_ELIGIBLE_MODEL",
                    "selection": selection,
                }
        result = self.start(self.selected)
        return {
            "status": "PASS",
            "action": "serve",
            "DIRECT_LOCAL_MODEL_STUDIO": "YES",
            "LOCAL_OPENAI_COMPATIBLE_ENDPOINT": "YES",
            "LMSTUDIO_SERVER_REQUIRED": "NO",
            "selection": selection,
            **result,
        }

    def bonsai_support_status(self) -> dict:
        """Report Bonsai availability without downloading or attempting to load a model.

        A llama.cpp binary can prove a Bonsai quantization is usable only by
        successfully loading that exact local GGUF.  Until then this method
        deliberately reports an unverified state instead of inferring support
        from the executable name or a version string.
        """
        bonsai_models = [item for item in self.profiles() if item.get("bonsai_model") == "YES"]
        executable = self.executable_path()
        prism = prism_bonsai_runtime_status(load_local_backend_config(self.config_path))
        if not bonsai_models:
            compatibility = "NOT_TESTED_NO_MODEL"
            status = "BONSAI_NOT_INSTALLED"
        elif prism["compatibility_status"] != "PASS":
            compatibility = "BONSAI_RUNTIME_UNKNOWN"
            status = "BONSAI_INSTALLED_RUNTIME_NOT_READY"
        elif executable is None:
            compatibility = "LLAMA_SERVER_NOT_FOUND"
            status = "BONSAI_INSTALLED_SERVER_MISSING"
        elif any(item.get("bonsai_minimal_smoke_status") == "PASS" for item in bonsai_models):
            compatibility = "COMPATIBLE_MODEL_SMOKE_PASSED"
            status = "BONSAI_INSTALLED"
        elif self.running() and self.selected and any(item.get("model_id") == self.selected.model_id for item in bonsai_models):
            compatibility = "COMPATIBLE_MODEL_LOADED"
            status = "BONSAI_INSTALLED"
        else:
            compatibility = "REQUIRES_LOCAL_LOAD_SMOKE"
            status = "BONSAI_INSTALLED_UNVERIFIED"
        bonsai_details = []
        for item in bonsai_models:
            bonsai_details.append({
                "model_id": item["model_id"],
                "format": item["bonsai_format"],
                "minimal_smoke_status": item.get("bonsai_minimal_smoke_status", "NOT_RUN"),
                "performance_class": item.get("bonsai_performance_class", "NOT_CLASSIFIED"),
                "recommended_use": item.get("bonsai_recommended_use", "NOT_APPLICABLE"),
                "auto_select_allowed": item.get("bonsai_auto_select_allowed", "NO"),
                "last_error_code": item.get("last_error_code") or "NONE",
            })
        return {
            "status": status,
            "BONSAI_NOT_INSTALLED": "YES" if not bonsai_models else "NO",
            "BONSAI_NOT_INSTALLED_HANDLED": "YES",
            "BONSAI_AUTO_DOWNLOAD": "NO",
            "BONSAI_NOT_REQUIRED_FOR_CURRENT_SMOKE": "YES",
            "BONSAI_NOT_DEFAULT_BEFORE_SMOKE": "YES",
            "llama_server_found": "YES" if executable else "NO",
            "llama_server_compatibility": compatibility,
            "prism_bonsai_runtime": prism,
            "models": bonsai_details,
        }

    def _persist_selected(self, model: GGUFModel) -> None:
        config = load_local_backend_config(self.config_path)
        config["selected_model_path"] = str(model.path)
        config["llama_server_path"] = str(self.executable_path() or config.get("llama_server_path") or "")
        runtimes = config.setdefault("runtimes", {})
        standard = runtimes.setdefault(STANDARD_RUNTIME_ID, {"runtime_id": STANDARD_RUNTIME_ID})
        standard["llama_server_path"] = config["llama_server_path"]
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

    def _command(self, model: GGUFModel, runtime_id: str = STANDARD_RUNTIME_ID) -> list[str]:
        config = load_local_backend_config(self.config_path)
        if runtime_id == PRISM_RUNTIME_ID:
            prism = prism_bonsai_runtime_status(config)
            if prism["compatibility_status"] != "PASS":
                raise ProviderError("BONSAI_RUNTIME_NOT_READY")
            executable = Path(str(prism["llama_server_path"])) if prism.get("llama_server_path") else None
        else:
            executable = self.executable_path()
        if executable is None:
            raise ProviderError("LLAMA_SERVER_NOT_FOUND")
        command = [str(executable), "-m", str(model.path), "--host", "127.0.0.1", "--port", str(self.port), "--ctx-size", str(self.ctx_size)]
        if runtime_id == PRISM_RUNTIME_ID:
            # Keep a tiny smoke response visible instead of spending its token
            # budget in the model template's reasoning channel.
            command.extend(["--reasoning", "off"])
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

    def port_in_use(self, port: int | None = None) -> bool:
        target_port = int(port or self.port)
        try:
            with socket.create_connection(("127.0.0.1", target_port), timeout=0.2):
                return True
        except OSError:
            return False

    def start(self, model: GGUFModel | None = None) -> dict:
        selected = model or self.selected
        if selected is None:
            discovered = self.discover()
            if not discovered:
                raise ProviderError("NO_GGUF_MODEL")
            raise ProviderError("MODEL_NOT_SELECTED")
        if not selected.path.is_file():
            raise ProviderError("NO_GGUF_MODEL")
        runtime_id, route_reason = runtime_for_model(selected, load_local_backend_config(self.config_path))
        if runtime_id is None:
            raise ProviderError(route_reason)
        if runtime_id == PRISM_RUNTIME_ID and prism_bonsai_runtime_status(load_local_backend_config(self.config_path))["compatibility_status"] != "PASS":
            raise ProviderError("BONSAI_RUNTIME_NOT_READY")
        if self.running():
            return self.status()
        if runtime_id == STANDARD_RUNTIME_ID and not self.executable_available():
            raise ProviderError("LLAMA_SERVER_NOT_FOUND")
        self._prepare_port()
        command = self._command(selected, runtime_id)
        router_user_dir().mkdir(parents=True, exist_ok=True)
        try:
            self.process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.pid_path.write_text(str(self.process.pid), encoding="utf-8")
            self.state_path.write_text(json.dumps({"selected_model_path": str(selected.path), "endpoint": self.endpoint(), "port": self.port, "runtime_id": runtime_id, "executable": command[0]}, ensure_ascii=False), encoding="utf-8")
        except OSError as exc:
            raise ProviderError("START_FAILED") from exc
        self.selected = selected
        if not self.wait_ready():
            self.stop()
            raise ProviderError("HEALTH_TIMEOUT")
        return self.status()

    def stop(self) -> dict:
        owner = self.port_owner(self.port) if self.process is None else None
        pid = self.process.pid if self.process is not None else (self._external_pid() or (owner or {}).get("pid"))
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
        elif pid and self._pid_alive(pid):
            if owner and owner.get("pid") == pid and self._owner_matches_backend(owner):
                self._terminate_owner(owner)
        self.process = None
        self._wait_port_free(self.port, timeout=5)
        self._clear_state_files()
        return self.status()

    def restart(self, model: GGUFModel | None = None) -> dict:
        self.stop()
        return self.start(model)

    def repair(self) -> dict:
        try:
            result = self.restart(self.selected) if self.running() else self.start(self.selected)
            response = self.ask("只回复 LOCAL_DIRECT_OK")
            return {"status": "PASS", "action": "repair", "response": response, **result}
        except ProviderError as exc:
            return {"status": "ERROR", "action": "repair", "error_code": str(exc), **self.status()}

    def auto_smoke(self, task: str, risk: str = "auto", mode: str = "auto") -> dict:
        started = time.monotonic()
        previous_model = self.selected.model_id if self.selected else None
        was_running = self.running()
        selection = self.auto_select(task, risk=risk, mode=mode, apply=True)
        selected_id = selection.get("selected_model")
        if not selected_id:
            return {"status": "ERROR", "error_code": selection.get("error_code") or "LOCAL_NO_ELIGIBLE_MODEL", "selection": selection, "model": None, "endpoint": self.endpoint()}
        chosen = next((item for item in self.discover() if item.model_id == selected_id), None)
        if chosen is None:
            return {"status": "ERROR", "error_code": "MODEL_NOT_FOUND", "selection": selection, "model": selected_id, "endpoint": self.endpoint()}
        try:
            if not was_running:
                self.start(chosen)
            elif previous_model != chosen.model_id:
                self.restart(chosen)
            response = self.ask(task)
            if not response:
                raise ProviderError("LOCAL_EMPTY_RESPONSE")
            elapsed = round(time.monotonic() - started, 3)
            self._record_profile_result(chosen.model_id, "PASS", elapsed, None)
            result = {"status": "PASS", "response": response, "model": chosen.model_id, "endpoint": self.endpoint(), "selection": selection, "duration_seconds": elapsed}
            return result
        except ProviderError as exc:
            code = str(exc)
            if code in {"HEALTH_TIMEOUT", "START_FAILED"}:
                code = "LOCAL_MODEL_LOAD_TIMEOUT"
            elif "TIMEOUT" in code.upper():
                code = "LOCAL_MODEL_LOAD_TIMEOUT" if code == "COMPLETION_TIMEOUT" else code
            elapsed = round(time.monotonic() - started, 3)
            self._record_profile_result(chosen.model_id, "TIMEOUT" if "TIMEOUT" in code else "FAIL", elapsed, code)
            result = {"status": "ERROR", "error_code": code, "model": chosen.model_id, "endpoint": self.endpoint(), "selection": selection, "duration_seconds": elapsed}
            return result

    def _record_profile_result(self, model_id: str, status: str, elapsed: float, error_code: str | None) -> None:
        config = load_local_backend_config(self.config_path)
        profiles = config.get("model_profiles") if isinstance(config.get("model_profiles"), dict) else {}
        profile = dict(profiles.get(model_id) or {})
        profile.update({"last_smoke_status": status, "last_latency_seconds": elapsed, "last_error_code": error_code})
        profiles[model_id] = profile
        config["model_profiles"] = profiles
        save_local_backend_config(config, self.config_path)
        try:
            from ..accounting.performance import PerformanceTracker
            PerformanceTracker.for_current_user().record(
                f"local:{model_id}", elapsed, status == "PASS", False, 20
            )
        except Exception:
            pass

    def record_bonsai_minimal_smoke(self, model_id: str, status: str, elapsed: float, error_code: str | None = None) -> dict:
        """Persist a measured bounded smoke result; Bonsai remains manual-only by policy."""
        selected = next((item for item in self.discover() if item.model_id == model_id), None)
        if selected is None or bonsai_format_from_filename(selected.path.name) == "UNKNOWN":
            raise ProviderError("BONSAI_MODEL_NOT_FOUND")
        performance_class, recommended_use = classify_bonsai_performance(status, elapsed, error_code)
        config = load_local_backend_config(self.config_path)
        profiles = config.get("model_profiles") if isinstance(config.get("model_profiles"), dict) else {}
        profile = dict(profiles.get(model_id) or {})
        profile.update({
            "last_smoke_status": status,
            "last_latency_seconds": elapsed,
            "last_error_code": error_code,
            "bonsai_minimal_smoke_status": status,
            "bonsai_performance_class": performance_class,
            "bonsai_recommended_use": recommended_use,
            "bonsai_auto_select_allowed": "NO",
        })
        profiles[model_id] = profile
        config["model_profiles"] = profiles
        save_local_backend_config(config, self.config_path)
        return {"model_id": model_id, "minimal_smoke": BONSAI_MINIMAL_SMOKE, **profile}

    def bonsai_minimal_smoke(self) -> dict:
        """Run an explicit, bounded Prism-only Bonsai completion smoke."""
        if self.selected is None or bonsai_format_from_filename(self.selected.path.name) == "UNKNOWN":
            raise ProviderError("BONSAI_MODEL_NOT_SELECTED")
        if runtime_for_model(self.selected, load_local_backend_config(self.config_path))[0] != PRISM_RUNTIME_ID:
            raise ProviderError("BONSAI_PRISM_RUNTIME_REQUIRED")
        original_ctx_size = self.ctx_size
        self.ctx_size = BONSAI_MINIMAL_SMOKE["ctx_size"]
        started = time.monotonic()
        try:
            if self.running():
                self.restart(self.selected)
            else:
                self.start(self.selected)
            provider = LMStudioProvider(
                self.endpoint(), self.selected.model_id,
                timeout=BONSAI_MINIMAL_SMOKE["timeout_seconds"],
                max_tokens=BONSAI_MINIMAL_SMOKE["max_tokens"],
            )
            response = provider.ask(BONSAI_MINIMAL_SMOKE["prompt"])
            elapsed = round(time.monotonic() - started, 3)
            status = "PASS" if response.strip() == "OK" else "FAIL"
            error_code = None if status == "PASS" else "BONSAI_MINIMAL_SMOKE_UNEXPECTED_RESPONSE"
            return {"status": status, "response": response, "duration_seconds": elapsed, **self.record_bonsai_minimal_smoke(self.selected.model_id, status, elapsed, error_code)}
        except ProviderError as exc:
            elapsed = round(time.monotonic() - started, 3)
            return {"status": "ERROR", "error_code": str(exc), "duration_seconds": elapsed, **self.record_bonsai_minimal_smoke(self.selected.model_id, "TIMEOUT" if "TIMEOUT" in str(exc).upper() else "FAIL", elapsed, str(exc))}
        finally:
            self.ctx_size = original_ctx_size

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

    def auto_ask(self, prompt: str, mode: str = "auto", risk: str = "auto") -> str:
        """Run a bounded local task through the GGUF selector when available."""
        if self.managed.executable_available() and (self.managed.selected or self.managed.discover()):
            result = self.managed.auto_smoke(prompt, risk=risk, mode=mode)
            if result.get("status") != "PASS":
                raise ProviderError(str(result.get("error_code") or "LOCAL_DIRECT_BACKEND_ERROR"))
            return str(result.get("response") or "")
        if self.lmstudio.available():
            return self.lmstudio.ask(prompt)
        raise ProviderError("LOCAL_DIRECT_BACKEND_NOT_CONFIGURED")

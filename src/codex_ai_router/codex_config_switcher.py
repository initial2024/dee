"""Safe, local Codex configuration switching with explicit file paths.

This module never reads Codex UI, sends model requests, or discovers secrets.
Callers must explicitly invoke a mutation method; tests use temporary configs.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import tomllib
from typing import Any

XIAOYU_PROVIDER = "XiaoyuRouter"
XIAOYU_ENDPOINT = "http://127.0.0.1:18789/v1"
class CodexConfigError(RuntimeError):
    pass


def default_config_path() -> Path:
    return Path(os.getenv("USERPROFILE") or Path.home()) / ".codex" / "config.toml"


def _read(path: Path) -> str:
    if not path.exists():
        raise CodexConfigError("CODEX_CONFIG_NOT_FOUND")
    return path.read_text(encoding="utf-8")


def _parse(text: str) -> dict[str, Any]:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise CodexConfigError("CODEX_CONFIG_TOML_INVALID") from exc


def _is_loopback(value: str) -> bool:
    return bool(re.fullmatch(r"http://(?:127\.0\.0\.1|localhost):18789/v1/?", value))


def _paths(root: Path) -> tuple[Path, Path, Path]:
    handoff = root / "codex-handoff"
    return handoff / "codex-config-backups", handoff / "codex-config-profiles", handoff / "codex-config-state.json"


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(path: Path, **updates: str) -> None:
    state = _load_state(path)
    state.update(updates)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def _safe_summary(path: Path, data: dict[str, Any]) -> dict[str, Any]:
    providers = data.get("model_providers") if isinstance(data.get("model_providers"), dict) else {}
    provider_id = data.get("model_provider") if isinstance(data.get("model_provider"), str) else "DEFAULT"
    provider = providers.get(provider_id) if isinstance(providers.get(provider_id), dict) else {}
    base_url = provider.get("base_url") if isinstance(provider.get("base_url"), str) else None
    return {"path": str(path), "model_provider": provider_id, "base_url": base_url if _is_loopback(base_url or "") else ("OFFICIAL_OR_UNMANAGED" if not base_url else "NON_LOOPBACK_BLOCKED"), "wire_api": provider.get("wire_api") if isinstance(provider.get("wire_api"), str) else None, "status": "XIAOYU_CUSTOM_ROUTER" if provider_id == XIAOYU_PROVIDER else "OFFICIAL_CODEX" if provider_id == "DEFAULT" else "UNKNOWN", "restart_codex_required": "YES"}


class CodexConfigSwitcher:
    def __init__(self, config_path: Path | None = None, root: Path | None = None) -> None:
        self.config_path = config_path or default_config_path()
        self.root = root or Path.cwd()

    def detect_config(self) -> dict[str, Any]:
        _, _, state_path = _paths(self.root)
        return {
            **_safe_summary(self.config_path, _parse(_read(self.config_path))),
            "last_backup_path": _load_state(state_path).get("last_backup", "NONE"),
            "last_switch_time": _load_state(state_path).get("last_switch_time", "NONE"),
        }

    def backup_config(self) -> dict[str, Any]:
        text = _read(self.config_path); _parse(text)
        backups, _, state_path = _paths(self.root); backups.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
        backup = backups / f"config-{stamp}.toml.bak"; shutil.copyfile(self.config_path, backup)
        meta = backups / f"config-{stamp}.meta.json"; meta.write_text(json.dumps({"created_at": datetime.now(timezone.utc).isoformat(), "source_name": self.config_path.name, "summary": self.detect_config()}, ensure_ascii=False), encoding="utf-8")
        _save_state(state_path, last_backup=str(backup), last_backup_time=datetime.now(timezone.utc).isoformat())
        return {"status": "BACKUP_CREATED", "backup_path": str(backup), "metadata_path": str(meta)}

    def capture_official_profile(self, *, user_confirmed: bool = False) -> dict[str, Any]:
        if not user_confirmed: return {"status": "USER_CONFIRMATION_REQUIRED"}
        text = _read(self.config_path); _parse(text)
        _, profiles, _ = _paths(self.root); profiles.mkdir(parents=True, exist_ok=True)
        target = profiles / "official.toml"; target.write_text(text, encoding="utf-8")
        _, _, state_path = _paths(self.root)
        _save_state(state_path, official_profile_captured_at=datetime.now(timezone.utc).isoformat())
        return {"status": "OFFICIAL_PROFILE_CAPTURED", "profile_path": str(target)}

    def _write_after_backup(self, text: str) -> dict[str, Any]:
        _parse(text); backup = self.backup_config(); original = _read(self.config_path)
        try: self.config_path.write_text(text, encoding="utf-8"); _parse(_read(self.config_path))
        except Exception:
            self.config_path.write_text(original, encoding="utf-8"); raise
        return backup

    def switch_to_xiaoyu_router(self, endpoint: str = XIAOYU_ENDPOINT) -> dict[str, Any]:
        if not _is_loopback(endpoint): raise CodexConfigError("NON_LOOPBACK_ENDPOINT_BLOCKED")
        lines = _read(self.config_path).splitlines(); _parse("\n".join(lines))
        first_section = next((i for i, line in enumerate(lines) if line.strip().startswith("[")), len(lines))
        # Only replace the root selector.  A similarly named key in an unknown
        # table belongs to that table and must remain untouched.
        lines = [line for i, line in enumerate(lines) if not (i < first_section and re.match(r"^\s*model_provider\s*=", line))]
        # Recalculate after removal so the selector remains a TOML top-level
        # key, rather than accidentally becoming part of the first provider.
        top_end = next((i for i, line in enumerate(lines) if line.strip().startswith("[")), len(lines))
        lines.insert(top_end, f'model_provider = "{XIAOYU_PROVIDER}"')
        section = f"[model_providers.{XIAOYU_PROVIDER}]"; start = next((i for i, line in enumerate(lines) if line.strip() == section), None)
        block = [section, 'name = "Xiaoyu Router"', f'base_url = "{endpoint}"', 'wire_api = "responses"', 'requires_openai_auth = false']
        if start is None: lines.extend([""] + block)
        else:
            end = next((i for i in range(start + 1, len(lines)) if lines[i].strip().startswith("[")), len(lines)); lines[start:end] = block
        backup = self._write_after_backup("\n".join(lines) + "\n")
        _, _, state_path = _paths(self.root)
        _save_state(state_path, last_switch_time=datetime.now(timezone.utc).isoformat(), last_action="switch-xiaoyu")
        return {**backup, **self.detect_config(), "status": "XIAOYU_ROUTER_ENABLED"}

    def switch_to_official_codex(self) -> dict[str, Any]:
        _, profiles, _ = _paths(self.root); profile = profiles / "official.toml"
        if not profile.exists(): return {"status": "OFFICIAL_PROFILE_NOT_CAPTURED"}
        backup = self._write_after_backup(profile.read_text(encoding="utf-8"))
        _, _, state_path = _paths(self.root)
        _save_state(state_path, last_switch_time=datetime.now(timezone.utc).isoformat(), last_action="switch-official")
        return {**backup, **self.detect_config(), "status": "OFFICIAL_PROFILE_RESTORED"}

    def restore_previous_config(self) -> dict[str, Any]:
        _, _, state = _paths(self.root)
        if not state.exists(): return {"status": "BACKUP_NOT_FOUND"}
        backup = Path(json.loads(state.read_text(encoding="utf-8")).get("last_backup", ""))
        if not backup.exists(): return {"status": "BACKUP_NOT_FOUND"}
        current = self.backup_config(); self.config_path.write_text(backup.read_text(encoding="utf-8"), encoding="utf-8"); _parse(_read(self.config_path))
        _save_state(state, last_switch_time=datetime.now(timezone.utc).isoformat(), last_action="restore-previous")
        return {**self.detect_config(), "status": "PREVIOUS_CONFIG_RESTORED", "pre_restore_backup": current["backup_path"]}

    def validate_config(self) -> dict[str, Any]:
        data = _parse(_read(self.config_path)); summary = _safe_summary(self.config_path, data); issues: list[str] = []
        providers = data.get("model_providers", {})
        provider = providers.get(data.get("model_provider"), {}) if isinstance(providers, dict) else {}
        if data.get("model_provider") == XIAOYU_PROVIDER:
            if provider.get("wire_api") != "responses": issues.append("WIRE_API_INVALID")
            if not _is_loopback(str(provider.get("base_url", ""))): issues.append("NON_LOOPBACK_ENDPOINT_BLOCKED")
        return {"status": "PASS" if not issues else "FAIL", "issues": issues, **summary, "codex_ui_scraping": "NO", "codex_ui_automation": "NO"}

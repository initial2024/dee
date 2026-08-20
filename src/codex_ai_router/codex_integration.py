from __future__ import annotations

from pathlib import Path
import os


def default_codex_config_path() -> Path:
    return Path(os.getenv("USERPROFILE") or Path.home()) / ".codex" / "config.toml"


def install_xiaoyu_router_provider(config_path: Path | None = None, port: int = 18789) -> dict:
    """Append only an independent custom-provider entry; never switch the active model/provider."""
    target = config_path or default_codex_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    original = target.read_text(encoding="utf-8") if target.exists() else ""
    section = "[model_providers.XiaoyuRouter]"
    if section in original:
        return {"status": "ALREADY_PRESENT", "path": str(target), "existing_provider_preserved": True}
    block = (
        "\n# Xiaoyu Router is localhost-only; downstream credentials remain in Router env references.\n"
        f"{section}\n"
        "name = \"Xiaoyu Router\"\n"
        f"base_url = \"http://127.0.0.1:{port}/v1\"\n"
        "wire_api = \"responses\"\n"
        "requires_openai_auth = false\n"
    )
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(original.rstrip() + "\n" + block, encoding="utf-8")
    temporary.replace(target)
    return {"status": "INSTALLED", "path": str(target), "existing_provider_preserved": True}


def same_thread_provider_switch_support() -> str:
    """Codex does not expose a supported same-thread provider-switch API to Router."""
    return "UNSUPPORTED"

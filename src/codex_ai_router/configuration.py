from __future__ import annotations

from pathlib import Path
import yaml


def load_config(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("router config must be a mapping")
    api = data.get("api", {})
    if "api_key" in api:
        raise ValueError("api_key is forbidden; use api_key_env")
    return data

from __future__ import annotations

import json
import re


def _object(raw: str) -> dict | None:
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else None
    except (TypeError, json.JSONDecodeError): return None


def parse_structured(raw: str) -> dict | None:
    if value := _object(raw.strip()): return value
    for block in re.findall(r"```(?:json)?\s*(.*?)```", raw, flags=re.I | re.S):
        if value := _object(block.strip()): return value
    decoder = json.JSONDecoder()
    found: list[dict] = []
    for match in re.finditer(r"\{", raw):
        try:
            value, _ = decoder.raw_decode(raw[match.start():])
        except json.JSONDecodeError: continue
        if isinstance(value, dict): found.append(value)
    if len(found) == 1: return found[0]
    cleaned = re.sub(r"^\s*```(?:json)?|```\s*$", "", raw.strip(), flags=re.I | re.S)
    cleaned = re.sub(r",\s*([}\]])", r"\1", cleaned)
    if value := _object(cleaned.strip()): return value
    repaired = [_object(re.sub(r",\s*([}\]])", r"\1", item)) for item in re.findall(r"\{[^{}]*\}", raw, flags=re.S)]
    repaired = [item for item in repaired if item is not None]
    return repaired[0] if len(repaired) == 1 else None


def readonly_task(task: str, risk: str) -> bool:
    words = task.lower()
    return risk == "LOW" and not any(word in words for word in ("edit", "write", "patch", "test", "command", "run", "delete"))

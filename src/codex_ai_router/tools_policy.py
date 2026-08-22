from __future__ import annotations

import json
import os
from pathlib import Path


STRICT_REJECT = "strict_reject"
TEXT_ONLY_STRIP = "text_only_strip"
MANUAL_PLAN = "manual_plan"
VALID_POLICIES = (STRICT_REJECT, TEXT_ONLY_STRIP, MANUAL_PLAN)

TEXT_ONLY_SYSTEM_INSTRUCTION = (
    "当前处于 TEXT_ONLY 兼容模式。\n"
    "你不能调用工具。\n"
    "你不能声称已经读取、修改、运行、提交或部署。\n"
    "你只能基于用户提供的上下文输出分析、计划、补丁建议、命令建议和 Codex 指令。\n"
    "如果需要真实文件操作，请明确要求用户切回 OFFICIAL_DIRECT 或交给 Codex 官方工具执行。\n"
    "请使用以下结构输出：Problem Summary、Analysis、Proposed Plan、Codex Instruction、Manual Commands、Risk Check、Requires Official Codex Tools。"
)


def policy_path() -> Path:
    configured = os.getenv("XIAOYU_ROUTER_TOOLS_POLICY_FILE")
    if configured:
        return Path(configured)
    return Path(os.getenv("USERPROFILE") or Path.home()) / ".codex-ai-router" / "tools-policy.json"


def normalize_policy(value: object) -> str:
    return value if isinstance(value, str) and value in VALID_POLICIES else STRICT_REJECT


def load_policy(path: Path | None = None) -> str:
    target = path or policy_path()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
        return normalize_policy(raw.get("codex_tools_policy") if isinstance(raw, dict) else None)
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return STRICT_REJECT


def save_policy(value: str, path: Path | None = None) -> dict[str, str]:
    policy = normalize_policy(value)
    target = path or policy_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps({"codex_tools_policy": policy}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)
    return {"codex_tools_policy": policy, "path": str(target)}


def request_has_tools(payload: dict) -> bool:
    return any(bool(payload.get(field)) for field in ("tools", "tool_choice", "function_call", "functions"))


def strip_tool_fields(payload: dict, *, chat: bool = False) -> dict:
    result = dict(payload)
    for field in ("tools", "tool_choice", "function_call", "functions"):
        result.pop(field, None)
    if chat:
        messages = list(result.get("messages") or [])
        messages.insert(0, {"role": "system", "content": TEXT_ONLY_SYSTEM_INSTRUCTION})
        result["messages"] = messages
    else:
        existing = result.get("instructions")
        result["instructions"] = TEXT_ONLY_SYSTEM_INSTRUCTION if not existing else TEXT_ONLY_SYSTEM_INSTRUCTION + "\n" + str(existing)
    return result


"""GGUF model profiling and conservative task-aware local selection.

This module deliberately uses filename metadata only.  It never downloads a
model, reads model contents, or invents capabilities that are not observable.
"""
from __future__ import annotations

from pathlib import Path
import re
from typing import Any, Iterable


def _yes(value: bool) -> str:
    return "YES" if value else "NO"


def _parameter_guess(filename: str) -> str:
    match = re.search(r"(?i)(?:^|[-_.])([0-9]{1,3}(?:\.[0-9]+)?)[bB](?:[-_.]|$)", filename)
    if match:
        return match.group(1) + "B"
    if re.search(r"(?i)(?:7b|8b)", filename):
        return "7B/8B"
    if re.search(r"(?i)(?:14b|15b)", filename):
        return "14B/15B"
    if re.search(r"(?i)(?:32b|34b|70b|72b)", filename):
        return "32B+"
    return "UNKNOWN"


def _family_and_roles(filename: str) -> tuple[str, list[str]]:
    lower = filename.lower()
    roles: list[str] = []
    if any(word in lower for word in ("stheno", "roleplay", "role-play", "creative")):
        roles.extend(("roleplay", "creative"))
    if any(word in lower for word in ("qwen", "instruct", "general")):
        roles.extend(("general", "instruct"))
    if any(word in lower for word in ("code", "coder", "coding", "starcoder", "deepseek-coder")):
        roles.append("coding")
    if not roles:
        roles.append("general")
    if "qwen" in lower:
        family = "Qwen"
    elif "deepseek" in lower:
        family = "DeepSeek"
    elif "llama" in lower:
        family = "Llama"
    elif "mistral" in lower:
        family = "Mistral"
    elif "stheno" in lower:
        family = "Stheno"
    else:
        family = "UNKNOWN"
    return family, sorted(set(roles))


def profile_from_model(model: Any, persisted: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return a stable, JSON-friendly profile for a discovered GGUF model."""
    path = Path(getattr(model, "path", ""))
    filename = str(getattr(model, "filename", path.name) or path.name)
    quantization = str(getattr(model, "quantization", "UNKNOWN") or "UNKNOWN").upper()
    projector = "mmproj" in filename.lower() or "vision-projector" in filename.lower()
    text_model = not projector
    family, roles = _family_and_roles(filename)
    if projector:
        roles = ["vision_projector"]
    parameter_guess = _parameter_guess(filename)
    heavy_quant = quantization in {"BF16", "F16", "F32"}
    size = int(getattr(model, "size_bytes", 0) or 0)
    if heavy_quant or parameter_guess in {"14B/15B", "32B+"}:
        speed, quality = "heavy", "high"
    elif quantization in {"Q4_K_S", "Q4_0", "Q4_1"}:
        speed, quality = "fast", "balanced"
    elif quantization.startswith("Q4") or quantization.startswith("Q5"):
        speed, quality = "balanced", "balanced"
    else:
        speed, quality = "unknown", "unknown"
    result: dict[str, Any] = {
        "model_id": str(getattr(model, "model_id", path.stem) or path.stem),
        "path": str(path),
        "filename": filename,
        "size_bytes": size,
        "quantization": quantization,
        "parameter_guess": parameter_guess,
        "family_guess": family,
        "text_model": _yes(text_model),
        "vision_projector": _yes(projector),
        "role_tags": roles,
        "speed_class": speed,
        "quality_class": quality,
        "manual_disabled": False,
        "manual_preferred": False,
        "manual_only": False,
        "last_smoke_status": "UNKNOWN",
        "last_latency_seconds": None,
        "last_error_code": None,
    }
    if isinstance(persisted, dict):
        for key in ("manual_disabled", "manual_preferred", "manual_only", "last_smoke_status", "last_latency_seconds", "last_error_code"):
            if key in persisted:
                result[key] = persisted[key]
    return result


def profiles_for_models(models: Iterable[Any], persisted: dict[str, dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    persisted = persisted or {}
    return [profile_from_model(model, persisted.get(str(getattr(model, "model_id", "")))) for model in models]


class LocalModelSelector:
    """Select one local model without allowing unsafe or expensive defaults."""

    def __init__(self, profiles: Iterable[dict[str, Any]], current_model: str | None = None, policy: dict[str, Any] | None = None):
        self.profiles = [dict(item) for item in profiles]
        self.current_model = current_model or ""
        self.policy = policy or {}

    @staticmethod
    def _risk(task: str, risk: str) -> str:
        if risk and risk.lower() != "auto":
            return risk.upper()
        lower = task.lower()
        if any(word in lower for word in ("production", "deploy", "credential", "api key", "password", "permission", "database migration", "delete user")):
            return "HIGH"
        if any(word in lower for word in ("refactor", "multiple files", "large", "complex")):
            return "HIGH"
        if any(word in lower for word in ("edit", "write", "implement", "fix", "run test")):
            return "MEDIUM"
        return "LOW"

    @staticmethod
    def _task_roles(task: str, mode: str) -> set[str]:
        lower = task.lower()
        if mode.lower() in {"roleplay", "creative"} or any(word in lower for word in ("roleplay", "角色扮演", "creative", "创作")):
            return {"roleplay", "creative"}
        if mode.lower() in {"code", "review"} or any(word in lower for word in ("code", "代码", "python", "bug", "review", "diff", "报错")):
            return {"coding", "general", "instruct"}
        return {"general", "instruct"}

    def select(self, task_text: str, risk: str = "auto", mode: str = "auto", allow_slow_local: bool = False, allow_bf16_auto: bool = False) -> dict[str, Any]:
        resolved_risk = self._risk(task_text, risk)
        roles = self._task_roles(task_text, mode)
        skipped: list[dict[str, str]] = []
        eligible: list[dict[str, Any]] = []
        denied_by_policy = {str(item) for item in self.policy.get("manual_disabled_models", []) if item}
        for profile in self.profiles:
            model_id = str(profile.get("model_id", ""))
            if str(profile.get("text_model", "YES")).upper() != "YES":
                skipped.append({"model": model_id, "reason": "MMPROJ_NOT_TEXT_MODEL"})
                continue
            if profile.get("manual_disabled") or model_id in denied_by_policy:
                skipped.append({"model": model_id, "reason": "MANUAL_DENY"})
                continue
            if profile.get("manual_only") and not profile.get("manual_preferred"):
                skipped.append({"model": model_id, "reason": "MANUAL_ONLY_NOT_REQUESTED"})
                continue
            quant = str(profile.get("quantization", "UNKNOWN")).upper()
            if quant in {"BF16", "F16", "F32"} and not allow_bf16_auto:
                skipped.append({"model": model_id, "reason": "BF16_AUTO_DISABLED"})
                continue
            parameter = str(profile.get("parameter_guess", "UNKNOWN")).upper()
            parameter_match = re.search(r"([0-9]+(?:\.[0-9]+)?)", parameter)
            parameter_size = float(parameter_match.group(1)) if parameter_match else 0.0
            if (parameter in {"14B/15B", "32B+"} or parameter_size >= 14.0) and not allow_slow_local:
                skipped.append({"model": model_id, "reason": "HEAVY_MODEL_REQUIRES_HIGH_THRESHOLD"})
                continue
            status = str(profile.get("last_smoke_status", "UNKNOWN")).upper()
            error = str(profile.get("last_error_code", "") or "").upper()
            if status in {"TIMEOUT", "FAIL"} or "TIMEOUT" in error:
                skipped.append({"model": model_id, "reason": "RUNTIME_COOLDOWN"})
                continue
            eligible.append(profile)
        if resolved_risk in {"HIGH", "CRITICAL"}:
            return {"selected_model": None, "why_selected": "高风险任务不允许本地模型执行写入。", "skipped_models": skipped, "switched_model": False, "requires_api_or_official_codex": True, "error_code": "LOCAL_HIGH_RISK_SAFE_STOP"}
        if not eligible:
            return {"selected_model": None, "why_selected": "没有符合本地安全策略的模型。", "skipped_models": skipped, "switched_model": False, "requires_api_or_official_codex": True, "error_code": "LOCAL_NO_ELIGIBLE_MODEL"}
        only = str(self.policy.get("manual_only_model") or "")
        if only:
            only_matches = [item for item in eligible if item.get("model_id") == only]
            if not only_matches:
                skipped.append({"model": only, "reason": "ONLY_USE_MODEL_UNAVAILABLE"})
                return {"selected_model": None, "why_selected": "只使用模型不可用或不符合策略。", "skipped_models": skipped, "switched_model": False, "requires_api_or_official_codex": True, "error_code": "LOCAL_ONLY_MODEL_UNAVAILABLE"}
            eligible = only_matches
        preferred = str(self.policy.get("manual_preferred_model") or "")
        preferred_matches = [item for item in eligible if item.get("model_id") == preferred]
        if preferred_matches:
            eligible = preferred_matches + [item for item in eligible if item not in preferred_matches]
        def score(item: dict[str, Any]) -> tuple[int, int, int, int]:
            tags = set(item.get("role_tags") or [])
            role_score = 30 if tags & roles else 0
            preferred_score = 50 if item.get("manual_preferred") or item.get("model_id") == preferred else 0
            speed_score = {"fast": 20, "balanced": 12, "unknown": 5, "heavy": 0}.get(str(item.get("speed_class")), 0)
            current_score = 5 if item.get("model_id") == self.current_model else 0
            return preferred_score + role_score + speed_score + current_score, role_score, speed_score, current_score
        chosen = sorted(eligible, key=score, reverse=True)[0]
        for item in eligible:
            if item is not chosen and item.get("model_id") not in {preferred, self.current_model}:
                skipped.append({"model": str(item.get("model_id")), "reason": "LOWER_TASK_SCORE"})
        complex_task = resolved_risk in {"MEDIUM", "HIGH", "COMPLEX"} and any(word in task_text.lower() for word in ("multiple files", "refactor", "complex", "architecture", "复杂"))
        return {"selected_model": chosen.get("model_id"), "why_selected": "按任务类型、风险、速度和人工偏好选择本地模型。", "skipped_models": skipped, "switched_model": chosen.get("model_id") != self.current_model, "requires_api_or_official_codex": bool(complex_task), "error_code": None}

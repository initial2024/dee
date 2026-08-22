"""Loopback-only HTTP facade for the confirmation-gated Local Agent.

The API deliberately delegates all execution decisions to :class:`LocalAgent`.
It never accepts arbitrary shell commands and never serializes request or
response bodies into records.  The HTTP server that mounts this controller is
also constrained to a loopback bind by ``server.RouterResponsesServer``.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .local_agent import LocalAgent, LocalAgentError


AGENT_API_HOST = "127.0.0.1"
AGENT_API_PORT = 18789
AGENT_API_BASE = f"http://{AGENT_API_HOST}:{AGENT_API_PORT}"
AGENT_API_SERVICE = "xiaoyu-router-agent-api"
AGENT_API_VERSION = "1.0"
MAX_AGENT_BODY_BYTES = 256 * 1024

_RECORD_FIELDS = (
    "id",
    "created_at",
    "brain_provider",
    "mode",
    "risk_level",
    "action",
    "status",
    "duration_ms",
    "error_code",
    "files_touched",
    "user_confirmed",
    "saved_body",
)


def _confirmation_matches(payload: dict[str, Any], plan_id: str) -> bool:
    """Require an explicit boolean or a plan-bound, non-secret confirmation token."""
    if payload.get("confirm") is True:
        return True
    token = payload.get("confirm_token")
    return isinstance(token, str) and token == f"CONFIRM:{plan_id}"


class AgentApiController:
    """Route safe JSON requests to a :class:`LocalAgent` instance."""

    def __init__(self, root: Path | None = None, agent: LocalAgent | None = None):
        self.root = (root or Path.cwd()).resolve()
        self.agent = agent or LocalAgent(self.root)

    def health(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "service": AGENT_API_SERVICE,
            "version": AGENT_API_VERSION,
            "agent_api": True,
            "capabilities": [
                "health",
                "plan",
                "readonly",
                "draft-patch",
                "apply",
                "test",
                "commit",
                "records",
            ],
            "bind_host": AGENT_API_HOST,
            "port": AGENT_API_PORT,
            "loopback_only": "YES",
            "codex_agent_used": "NO",
            "codex_agentic_usage_required": "NO",
            "auto_file_modify": "NO",
            "auto_command_execute": "NO",
            "auto_commit": "NO",
            "auto_push": "NO",
            "auto_deploy": "NO",
            "public_exposure": "NO",
            "lan_exposure": "NO",
            "secrets_logged": "NO",
            "prompt_response_logged_by_default": "NO",
        }

    def records(self) -> dict[str, Any]:
        """Return metadata-only records; never return plan or body contents."""
        records: list[dict[str, Any]] = []
        try:
            lines = self.agent.ledger.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        for line in lines[-100:]:
            try:
                raw = json.loads(line)
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(raw, dict):
                continue
            records.append({key: raw.get(key) for key in _RECORD_FIELDS if key in raw})
        return {"records": records, "count": len(records), "metadata_only": "YES", "secrets_logged": "NO"}

    @staticmethod
    def _plan_id(payload: dict[str, Any]) -> str:
        value = payload.get("plan_id", payload.get("plan", ""))
        return value if isinstance(value, str) else ""

    def _confirmed(self, payload: dict[str, Any], plan_id: str) -> bool:
        return _confirmation_matches(payload, plan_id)

    def _dispatch(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        if path == "/agent/plan":
            task = payload.get("task")
            if not isinstance(task, str) or not task.strip():
                raise LocalAgentError("TASK_REQUIRED")
            provider = payload.get("brain_provider", "local-light")
            risk = payload.get("risk", "auto")
            invoke = payload.get("invoke_brain") is True
            result = self.agent.plan(task, brain_provider=provider, risk=risk, invoke_brain_now=invoke)
            result["api_endpoint"] = "/agent/plan"
            result["confirmation_token_format"] = "CONFIRM:<plan_id>"
            return result

        if path == "/agent/readonly":
            task = payload.get("task", "")
            plan_id = self._plan_id(payload) or None
            return self.agent.readonly(task if isinstance(task, str) else "", plan_id)

        plan_id = self._plan_id(payload)
        if not plan_id:
            raise LocalAgentError("PLAN_ID_REQUIRED")

        if path == "/agent/draft-patch":
            # Draft creation is still a local artifact write, so it uses the
            # same explicit confirmation gate as the CLI.
            return self.agent.draft_patch(
                plan_id,
                confirm=self._confirmed(payload, plan_id),
                patch_text=payload.get("patch_text", "") if isinstance(payload.get("patch_text", ""), str) else "",
            )

        if path == "/agent/apply":
            patch_file = payload.get("patch_file")
            if not isinstance(patch_file, str) or not patch_file.strip():
                raise LocalAgentError("PATCH_FILE_REQUIRED")
            return self.agent.apply(plan_id, patch_file, confirm=self._confirmed(payload, plan_id))

        if path == "/agent/test":
            test_name = payload.get("test", "python-unittest")
            if not isinstance(test_name, str):
                raise LocalAgentError("TEST_NOT_ALLOWLISTED")
            return self.agent.test(plan_id, test_name=test_name, confirm=self._confirmed(payload, plan_id))

        if path == "/agent/commit":
            files = payload.get("files", payload.get("file", []))
            if isinstance(files, str):
                files = [files]
            if not isinstance(files, list) or not all(isinstance(item, str) for item in files):
                raise LocalAgentError("COMMIT_FILES_REQUIRED")
            message = payload.get("message", "")
            if not isinstance(message, str):
                raise LocalAgentError("COMMIT_MESSAGE_DENIED")
            return self.agent.commit(plan_id, files, message, confirm=self._confirmed(payload, plan_id))

        raise LocalAgentError("NOT_FOUND")

    def post(self, path: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        try:
            return 200, self._dispatch(path, payload)
        except LocalAgentError as exc:
            code = exc.code
            status = 403 if code == "LOCAL_AGENT_HIGH_RISK_STOP" else 400
            return status, {
                "status": "DENIED" if status == 403 else "ERROR",
                "error_code": code,
                "codex_agent_used": "NO",
                "auto_file_modify": "NO",
                "auto_command_execute": "NO",
                "auto_push": "NO",
                "auto_deploy": "NO",
                "secrets_logged": "NO",
            }
        except (OSError, TypeError, ValueError):
            return 400, {"status": "ERROR", "error_code": "INVALID_REQUEST"}

    def get(self, path: str) -> tuple[int, dict[str, Any]]:
        if path == "/agent/health":
            return 200, self.health()
        if path == "/agent/records":
            return 200, self.records()
        return 404, {"error": {"code": "not_found"}}


__all__ = ["AGENT_API_BASE", "AGENT_API_HOST", "AGENT_API_PORT", "AgentApiController", "MAX_AGENT_BODY_BYTES"]

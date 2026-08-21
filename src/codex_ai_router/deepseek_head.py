"""Local-only DeepSeek Head advisory adapter.

This module only talks to the already-running loopback Browser Bridge Worker.
It has no DeepSeek credential, private endpoint, browser profile, or file
mutation capability.  Callers choose when to invoke it.
"""
from __future__ import annotations

import json
import time
import uuid
import argparse
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .response_compat import ResponseCompatibilityError, normalize_chat_completion


LOCAL_API = "http://127.0.0.1:8792/v1"
LOCAL_HEALTH = "http://127.0.0.1:8791/health"


def _loopback(url: str) -> bool:
    return url.startswith("http://127.0.0.1:") or url.startswith("http://localhost:")


def _read_json(request: Request | str, timeout: int, opener: Callable[..., Any]) -> tuple[int, dict[str, Any]]:
    with opener(request, timeout=timeout) as response:
        raw = json.loads(response.read().decode("utf-8"))
        return int(getattr(response, "status", 200)), raw if isinstance(raw, dict) else {}


def advisory_result(text: str, task_type: str) -> dict[str, Any]:
    """Present visible output as advice only; it never generates executable actions."""
    return {
        "analysis": text,
        "recommended_next_step": "Review this advisory result in Codex before changing files or running commands.",
        "codex_instruction": f"Review the following DeepSeek Head advisory for a {task_type} task. Verify claims, keep changes scoped, and require confirmation before destructive or external actions.\n\n{text}",
        "powershell_command": "# No command is generated automatically. Review the advisory in Codex first.",
        "risk_notice": "Advisory only: no file, Git, deployment, or shell action has been performed.",
        "needs_human_confirmation": True,
    }


def run_deepseek_head(
    task: str,
    task_type: str = "AUTO",
    *,
    api_base: str = LOCAL_API,
    health_url: str = LOCAL_HEALTH,
    api_key: str = "",
    timeout: int = 90,
    opener: Callable[..., Any] = urlopen,
) -> dict[str, Any]:
    """Run one explicit, non-streaming advisory request over loopback only."""
    request_id = "head_" + uuid.uuid4().hex
    started = time.monotonic()
    base = api_base.rstrip("/")
    if not task.strip() or not _loopback(base) or not _loopback(health_url):
        return {"request_id": request_id, "status": "INVALID_LOCAL_REQUEST", "error_code": "INVALID_LOCAL_REQUEST", "duration_ms": 0}
    try:
        _status, health = _read_json(health_url, min(timeout, 5), opener)
    except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError):
        return {"request_id": request_id, "status": "BRIDGE_OFFLINE", "error_code": "BRIDGE_OFFLINE", "duration_ms": round((time.monotonic() - started) * 1000)}
    if health.get("busy") is True:
        return {"request_id": request_id, "status": "BRIDGE_BUSY", "error_code": "BRIDGE_BUSY", "duration_ms": round((time.monotonic() - started) * 1000)}
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    payload = {"model": "deepseek-web", "messages": [{"role": "user", "content": task}], "stream": False}
    try:
        _status, upstream = _read_json(Request(base + "/chat/completions", data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST"), timeout, opener)
        normalized = normalize_chat_completion(upstream, model="deepseek-web")
        result = advisory_result(normalized["choices"][0]["message"]["content"], task_type)
        return {"request_id": request_id, "status": "PASS", "model": "deepseek-web", "stream_mode": "non_stream", "duration_ms": round((time.monotonic() - started) * 1000), **result}
    except ResponseCompatibilityError:
        return {"request_id": request_id, "status": "UPSTREAM_CONTENT_EMPTY", "error_code": "UPSTREAM_CONTENT_EMPTY", "duration_ms": round((time.monotonic() - started) * 1000)}
    except HTTPError as exc:
        return {"request_id": request_id, "status": "UPSTREAM_HTTP_ERROR", "error_code": "UPSTREAM_HTTP_" + str(exc.code), "duration_ms": round((time.monotonic() - started) * 1000)}
    except (URLError, TimeoutError, OSError, json.JSONDecodeError):
        return {"request_id": request_id, "status": "BRIDGE_REQUEST_FAILED", "error_code": "BRIDGE_REQUEST_FAILED", "duration_ms": round((time.monotonic() - started) * 1000)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Local-only DeepSeek Head advisory")
    parser.add_argument("--task", required=True)
    parser.add_argument("--task-type", default="AUTO")
    parser.add_argument("--api-key", default="")
    args = parser.parse_args()
    print(json.dumps(run_deepseek_head(args.task, args.task_type, api_key=args.api_key), ensure_ascii=False))


if __name__ == "__main__":
    main()

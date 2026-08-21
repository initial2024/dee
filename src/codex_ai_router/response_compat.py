"""Secret-free OpenAI response shape compatibility helpers.

The functions here only transform already-received payloads.  They never log,
persist, or submit request content.
"""
from __future__ import annotations

import json
import time
import uuid
from collections.abc import Iterable, Iterator
from typing import Any


class ResponseCompatibilityError(ValueError):
    """A successful upstream response did not contain visible assistant text."""

    code = "UPSTREAM_CONTENT_EMPTY"


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("text", "output_text", "content", "value", "output", "completion", "result", "answer"):
            result = _text(value.get(key))
            if result:
                return result
        return ""
    if isinstance(value, list):
        return "".join(_text(item) for item in value)
    return ""


def extract_visible_text(payload: Any) -> str:
    """Extract text from common Chat Completions and Responses API shapes."""
    if not isinstance(payload, dict):
        return ""
    choices = payload.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        choice = choices[0]
        value = _text(choice.get("message")) or _text(choice.get("delta"))
        if value:
            return value
    value = (_text(payload.get("output_text")) or _text(payload.get("content")) or
             _text(payload.get("message")) or _text(payload.get("text")) or
             _text(payload.get("completion")) or _text(payload.get("result")) or
             _text(payload.get("answer")))
    if value:
        return value
    return _text(payload.get("output")) or _text(payload.get("response"))


def normalize_chat_completion(payload: dict[str, Any], model: str | None = None, created: int | None = None) -> dict[str, Any]:
    text = extract_visible_text(payload)
    if not text:
        raise ResponseCompatibilityError(ResponseCompatibilityError.code)
    return {
        "id": str(payload.get("id") or "chatcmpl_xiaoyu_" + uuid.uuid4().hex),
        "object": "chat.completion",
        "created": int(created if created is not None else payload.get("created") or time.time()),
        "model": str(model or payload.get("model") or "unknown"),
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": payload.get("usage") if isinstance(payload.get("usage"), dict) else None,
    }


def _frame(payload: dict[str, Any]) -> str:
    return "data: " + json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n\n"


def iter_normalized_sse(source: Iterable[str | bytes], model: str = "unknown", created: int | None = None) -> Iterator[str]:
    """Incrementally map upstream SSE data lines to OpenAI chat chunks.

    The iterator consumes one source line at a time and never materializes the
    full stream.  It emits a terminal chunk and ``[DONE]`` once.
    """
    response_id = "chatcmpl_xiaoyu_" + uuid.uuid4().hex
    timestamp = int(created if created is not None else time.time())
    content_seen = False
    terminal_sent = False
    for raw_line in source:
        line = raw_line.decode("utf-8", errors="replace") if isinstance(raw_line, bytes) else raw_line
        if not line.startswith("data:"):
            continue
        raw = line[5:].strip()
        if not raw:
            continue
        if raw == "[DONE]":
            break
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and isinstance(event.get("error"), dict):
            yield _frame({"error": {"code": event["error"].get("code", "UPSTREAM_ERROR")}})
            terminal_sent = True
            break
        text = extract_visible_text(event)
        if text:
            content_seen = True
            yield _frame({"id": response_id, "object": "chat.completion.chunk", "created": timestamp, "model": model, "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]})
    if not terminal_sent:
        if not content_seen:
            yield _frame({"error": {"code": ResponseCompatibilityError.code}})
        yield _frame({"id": response_id, "object": "chat.completion.chunk", "created": timestamp, "model": model, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
    yield "data: [DONE]\n\n"


def diagnostic_headers(*, upstream_status: int | str, upstream_content_type: str, endpoint_mode: str, normalized: bool, content_detected: bool, stream_mode: str) -> dict[str, str]:
    """Return diagnostic metadata only; values deliberately exclude content."""
    return {
        "X-Xiaoyu-Upstream-Status": str(upstream_status),
        "X-Xiaoyu-Upstream-Content-Type": upstream_content_type or "unknown",
        "X-Xiaoyu-Endpoint-Mode": endpoint_mode,
        "X-Xiaoyu-Normalized": "yes" if normalized else "no",
        "X-Xiaoyu-Content-Detected": "yes" if content_detected else "no",
        "X-Xiaoyu-Stream-Mode": stream_mode,
    }

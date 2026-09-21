"""Safe, single-request Router-assisted response capture harness.

The default fixture mode starts an in-process loopback HTTP server and never
starts a Router, Bridge, browser, or model.  Live mode is intentionally
double-gated and is reserved for a separately authorized single request.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


TASK_NAME = "XIAOYU-ROUTER-P1W10X-DEEPSEEK-L4-R8E-IMPLEMENT-CAPTURE-HARNESS-NO-MODEL-CALL"
FIXTURE_MARKER = "R8E_FIXTURE_CAPTURE_OK"
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


@dataclass(frozen=True)
class CaptureResult:
    http_status: int | None
    exit_code: int
    body: bytes
    error_type: str | None = None


def _loopback_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "http" and (parsed.hostname or "").lower() in _LOOPBACK_HOSTS


def _allowlisted_summary(
    result: CaptureResult,
    *,
    mode: str,
    marker: str,
    stdout: str,
    stderr: str,
) -> dict[str, Any]:
    body: dict[str, Any] | None = None
    try:
        decoded = json.loads(result.body.decode("utf-8"))
        body = decoded if isinstance(decoded, dict) else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        body = None

    marker_found = bool(
        body
        and (
            body.get("response_marker") == marker
            or (
                body.get("response_contains_marker") is True
                and body.get("prompt_marker_expected") == marker
            )
        )
    )
    provider = None if body is None else body.get("selected_brain") or body.get("router_selected_backend")
    success_telemetry = bool(body and body.get("bridge_send_attempted") == "YES" and body.get("status") == "PASS")
    capability_metadata_ok = bool(body and body.get("router_capability_metadata_sent") is True)
    fallback = None if body is None else body.get("fallback_triggered")
    if not isinstance(fallback, bool):
        fallback = False if body and body.get("fallback_reason") in {None, "", "NONE"} else None

    return {
        "TASK_NAME": TASK_NAME,
        "mode": mode,
        "request_sent": "YES" if mode == "live" else "YES_LOCAL_FIXTURE_ONLY",
        "model_call_sent": "YES" if mode == "live" else "NO",
        "deepseek_request_sent": "YES" if mode == "live" else "NO",
        "http_status": result.http_status,
        "exit_code": result.exit_code,
        "stdout_captured": True,
        "stderr_captured": True,
        "stdout_bytes": len(stdout.encode("utf-8")),
        "stderr_bytes": len(stderr.encode("utf-8")),
        "response_body_captured": bool(result.body),
        "response_body_bytes": len(result.body),
        "router_json_parsed": body is not None,
        "marker_found": marker_found,
        "provider_selected": provider if provider in {"deepseek-bridge-direct", "deepseek-web"} else "unknown",
        "fallback_triggered": fallback,
        "success_telemetry": success_telemetry,
        "capability_metadata_ok": capability_metadata_ok,
        "error_type": result.error_type,
        "error_message_allowlisted": "NONE" if result.error_type is None else "HTTP_OR_TRANSPORT_CAPTURE_FAILED",
        "temp_files_used": False,
        "temp_files_cleaned": True,
    }


def capture_json_post(url: str, payload: dict[str, Any], timeout: float = 15.0) -> CaptureResult:
    """Issue exactly one local HTTP POST and retain its full response body in memory."""
    if not _loopback_url(url):
        return CaptureResult(None, 2, b"", "NON_LOOPBACK_URL_REJECTED")
    request = Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return CaptureResult(response.status, 0, response.read())
    except HTTPError as exc:
        return CaptureResult(exc.code, 1, exc.read(), "HTTP_ERROR")
    except (URLError, OSError, TimeoutError) as exc:
        return CaptureResult(None, 1, b"", type(exc).__name__)


class _FixtureHandler(BaseHTTPRequestHandler):
    marker = FIXTURE_MARKER

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_POST(self) -> None:
        if self.path != "/deepseek-head/coordinate":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        body = {
            "status": "PASS",
            "selected_brain": "deepseek-bridge-direct",
            "fallback_triggered": False,
            "bridge_send_attempted": "YES",
            "router_capability_metadata_sent": True,
            "response_contains_marker": True,
            "prompt_marker_expected": self.marker,
        }
        raw = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def run_fixture(marker: str = FIXTURE_MARKER) -> dict[str, Any]:
    """Exercise the capture path with only an in-process loopback fixture."""
    _FixtureHandler.marker = marker
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FixtureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    stdout, stderr = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = capture_json_post(
                f"http://127.0.0.1:{server.server_address[1]}/deepseek-head/coordinate",
                {"fixture": True, "task": "FIXTURE_ONLY_NO_MODEL_CALL"},
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    return _allowlisted_summary(result, mode="fixture", marker=marker, stdout=stdout.getvalue(), stderr=stderr.getvalue())


def run_live(router_url: str, task: str, marker: str) -> dict[str, Any]:
    """Reserved single non-streaming Router request; do not call without authorization."""
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        result = capture_json_post(
            router_url.rstrip("/") + "/deepseek-head/coordinate",
            {
                "task": task,
                "brain_provider": "deepseek-bridge-direct",
                "invoke_brain": True,
                "search": False,
                "collect_context": False,
                "allow_patch_draft": False,
                "allow_apply": False,
                "allow_commit": False,
                "allow_test": False,
            },
            timeout=140,
        )
    return _allowlisted_summary(result, mode="live", marker=marker, stdout=stdout.getvalue(), stderr=stderr.getvalue())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Router-assisted response capture harness")
    parser.add_argument("--live", action="store_true", help="reserved; requires the explicit acknowledgement flag")
    parser.add_argument("--i-understand-this-sends-one-model-request", action="store_true")
    parser.add_argument("--router-url", default="http://127.0.0.1:18789")
    parser.add_argument("--task")
    parser.add_argument("--marker", default=FIXTURE_MARKER)
    args = parser.parse_args(argv)

    if args.live:
        if not args.i_understand_this_sends_one_model_request or not args.task:
            parser.error("--live requires --i-understand-this-sends-one-model-request and --task")
        summary = run_live(args.router_url, args.task, args.marker)
    else:
        summary = run_fixture(args.marker)
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return int(summary["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())

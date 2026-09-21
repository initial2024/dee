from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from codex_ai_router.agent_api import AgentApiController
from codex_ai_router.assist_coordinator import AssistCoordinator
from codex_ai_router.deepseek_head_coordinator import DeepSeekHeadCoordinator
from codex_ai_router.local_agent import LocalAgent
from codex_ai_router.network import NetworkMode
from codex_ai_router.server import RouterResponsesServer, RouterService


class _FakeBridge(BaseHTTPRequestHandler):
    mode = "success"
    requests: list[dict] = []

    def log_message(self, _format: str, *_args: object) -> None:
        pass

    def _reply(self, status: int, body: dict) -> None:
        raw = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._reply(200, {"ok": True, "bridge": {"busy": False, "uiSendAttemptCount": 0}, "deepseek": {"loggedIn": True, "pageReady": True, "inputReady": True}})
        elif self.path == "/mode-probe":
            self._reply(200, {"ui_generation": "three_in_one", "reasoning_axis_type": "binary_toggle", "available_reasoning_strengths": ["off", "medium"], "max_available_reasoning_strength": "medium", "high_reasoning_supported": False, "max_reasoning_supported": False, "current_reasoning_strength": "medium", "current_search": False, "current_profile_label": "medium/no-search", "search_available": True, "ui_changed": False, "modes": {}})
        else:
            self._reply(404, {"error": {"code": "NOT_FOUND"}})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        _FakeBridge.requests.append(json.loads(self.rfile.read(length).decode("utf-8")))
        if _FakeBridge.mode == "failure":
            self._reply(422, {"error": {"code": "FAKE_BRIDGE_PRESEND_VALIDATION_FAILED", "bridge_error_code": "FAKE_BRIDGE_PRESEND_VALIDATION_FAILED", "bridge_stage": "pre_send_validation", "bridge_reason": "sanitized fake reason"}})
            return
        self._reply(200, {"id": "fake", "object": "chat.completion", "choices": [{"message": {"role": "assistant", "content": "XIAOYU_ROUTER_SMOKE_OK"}}]})


class DeepSeekLiveEndpointPassthroughTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name) / "repo"
        root.mkdir()
        agent = LocalAgent(root, Path(self.temp.name) / "agent")
        snapshot = lambda: [{"id": "deepseek-web-bridge", "type": "DEEPSEEK_WEB_BRIDGE", "enabled": True, "status": "ENABLED"}]
        controller = AgentApiController(root, agent, coordinator=AssistCoordinator(provider_snapshot=snapshot))
        controller.deepseek_head = DeepSeekHeadCoordinator(root, agent=agent, provider_snapshot=snapshot)
        self.router = RouterResponsesServer(RouterService(NetworkMode.OFFLINE), port=0, agent_api=controller)
        self.router.start()
        self.router_url = f"http://127.0.0.1:{self.router.httpd.server_address[1]}"
        _FakeBridge.mode = "success"
        _FakeBridge.requests = []
        self.bridge = ThreadingHTTPServer(("127.0.0.1", 8791), _FakeBridge)
        self.bridge_thread = threading.Thread(target=self.bridge.serve_forever, daemon=True)
        self.bridge_thread.start()

    def tearDown(self) -> None:
        self.bridge.shutdown()
        self.bridge.server_close()
        self.router.stop()
        self.temp.cleanup()

    def _coordinate(self) -> tuple[int, dict]:
        payload = {"task": "minimal fake bridge smoke", "brain_provider": "deepseek-bridge-direct", "invoke_brain": True, "search": False, "collect_context": False, "allow_patch_draft": False, "allow_apply": False, "allow_commit": False, "allow_test": False}
        request = Request(self.router_url + "/deepseek-head/coordinate", data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_live_endpoint_success_sends_constrained_capability_metadata(self) -> None:
        status, body = self._coordinate()
        self.assertEqual(status, 200)
        self.assertEqual(body["bridge_send_attempted"], "YES")
        sent = _FakeBridge.requests[-1]
        self.assertEqual(sent["target_profile"], "best_available_reasoning")
        self.assertEqual(sent["capability_snapshot"]["reasoning_axis_type"], "binary_toggle")
        self.assertEqual((sent["allow_search"], sent["allow_files"], sent["allow_vision"], sent["disallow_silent_high_max_fallback"]), (False, False, False, True))
        self.assertNotIn("high", sent["target_profile"])
        self.assertNotIn("max", sent["target_profile"])

    def test_live_endpoint_preserves_fake_pre_send_diagnostics(self) -> None:
        _FakeBridge.mode = "failure"
        status, body = self._coordinate()
        self.assertEqual(status, 200)
        self.assertNotEqual(body["error_code"], "DEEPSEEK_PROVIDER_ERROR")
        self.assertEqual(body["bridge_error_code"], "FAKE_BRIDGE_PRESEND_VALIDATION_FAILED")
        self.assertEqual(body["bridge_stage"], "pre_send_validation")
        self.assertEqual(body["bridge_reason"], "sanitized fake reason")
        self.assertEqual(body["bridge_ui_send_attempt_count"], 0)
        self.assertEqual(body["requested_profile"], "best_available_reasoning")
        self.assertEqual(body["resolved_profile"], "best_available_reasoning")
        self.assertEqual(body["router_selected_profile"], "best_available_reasoning")
        self.assertEqual(body["router_capability_metadata_sent"], "YES")
        self.assertEqual(body["reasoning_axis_type"], "binary_toggle")
        self.assertEqual(body["available_reasoning_strengths"], ["off", "medium"])
        self.assertEqual(body["max_available_reasoning_strength"], "medium")
        self.assertEqual((body["allow_search"], body["allow_files"], body["allow_vision"], body["disallow_silent_high_max_fallback"]), (False, False, False, True))
        self.assertEqual(body["http_post_to_bridge_attempted"], "YES")


if __name__ == "__main__":
    unittest.main()

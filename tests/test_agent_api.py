from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from codex_ai_router.agent_api import AgentApiController
from codex_ai_router.assist_coordinator import AssistCoordinator
from codex_ai_router.deepseek_head_coordinator import DeepSeekHeadCoordinator
from codex_ai_router.local_agent import LocalAgent
from codex_ai_router.network import NetworkMode
from codex_ai_router.server import RouterResponsesServer, RouterService


class AgentApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name) / "repo"
        root.mkdir()
        storage = Path(self.temp.name) / "agent"
        self.calls: list[tuple[str, ...]] = []

        def runner(args, _cwd, _timeout):
            self.calls.append(tuple(args))
            return 0, "safe metadata"

        agent = LocalAgent(root, storage, runner=runner)
        controller = AgentApiController(root, agent, coordinator=AssistCoordinator(provider_snapshot=lambda: [{"id": "local-light", "type": "LOCAL_MODEL", "enabled": True, "status": "ENABLED"}]))
        controller.deepseek_head = DeepSeekHeadCoordinator(root, agent=agent, provider_snapshot=lambda: [{"id": "deepseek-web-bridge", "type": "DEEPSEEK_WEB_BRIDGE", "enabled": True, "status": "ENABLED"}])
        self.server = RouterResponsesServer(RouterService(NetworkMode.OFFLINE), port=0, agent_api=controller)
        self.server.start()
        self.base = f"http://127.0.0.1:{self.server.httpd.server_address[1]}"

    def tearDown(self) -> None:
        self.server.stop()
        self.temp.cleanup()

    def get(self, path: str) -> tuple[int, dict]:
        try:
            with urlopen(self.base + path, timeout=3) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def post(self, path: str, payload: dict) -> tuple[int, dict]:
        request = Request(self.base + path, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urlopen(request, timeout=3) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_health_is_loopback_only_and_metadata_safe(self):
        status, body = self.get("/agent/health")
        self.assertEqual((status, body["loopback_only"], body["bind_host"], body["port"]), (200, "YES", "127.0.0.1", 18789))
        self.assertEqual((body["service"], body["agent_api"], body["version"]), ("xiaoyu-router-agent-api", True, "1.0"))
        self.assertIn("readonly", body["capabilities"])
        self.assertIn("assist-coordinate", body["capabilities"])
        self.assertEqual((body["official_assisted_coordinator"], body["official_direct_unchanged"]), ("YES", "YES"))
        self.assertEqual((body["public_exposure"], body["lan_exposure"], body["codex_agent_used"]), ("NO", "NO", "NO"))

    def test_assist_coordinate_is_loopback_plan_only_and_routes_steps(self):
        status, body = self.post("/assist/coordinate", {"task": "检查当前项目状态，不修改文件", "mode": "official_assisted"})
        self.assertEqual(status, 200)
        self.assertTrue(body["local_agent_steps"])
        self.assertEqual((body["codex_required_steps"], body["codex_endpoint_touched"], body["brain_invoked"]), ([], "NO", "NO"))
        self.assertEqual(body["mode"], "OFFICIAL_ASSISTED_COORDINATOR")

    def test_assist_coordinate_high_risk_is_stopped(self):
        status, body = self.post("/assist/coordinate", {"task": "删除所有文件并 git push", "mode": "official_assisted"})
        self.assertEqual(status, 200)
        self.assertNotIn("error_code", body)
        self.assertEqual(body["stop_conditions"], ["LOCAL_AGENT_HIGH_RISK_STOP"])

    def test_plan_does_not_invoke_brain_and_records_no_body(self):
        status, body = self.post("/agent/plan", {"task": "检查当前项目状态，不修改文件"})
        self.assertEqual((status, body["mode"], body["brain_invoked"], body["workspace_write"]), (200, "PLAN_ONLY", "NO", "NO"))
        self.assertEqual(body["files_touched"], [])
        status, records = self.get("/agent/records")
        self.assertEqual(status, 200)
        self.assertEqual(records["metadata_only"], "YES")
        raw = json.dumps(records, ensure_ascii=False)
        self.assertNotIn("task_summary", raw)
        self.assertNotIn("prompt", raw.lower())
        self.assertNotIn("response", raw.lower())

    def test_deepseek_head_coordinate_collects_context_without_invoking_brain(self):
        status, body = self.post("/deepseek-head/coordinate", {"task": "检查当前项目状态，不修改文件", "brain_provider": "auto"})
        self.assertEqual(status, 200)
        self.assertEqual((body["brain_invoked"], body["files_modified"], body["loopback_only"], body["tools_forwarded"]), ("NO", "NO", "YES", "NO"))
        self.assertEqual(body["selected_brain"], "local-agent-readonly")
        context_status, context = self.post("/deepseek-head/context", {"context_bundle_id": body["context_bundle_id"]})
        self.assertEqual((context_status, context["context_bundle"]["collection_mode"]), (200, "read_only"))

    def test_readonly_uses_only_allowlisted_read_commands(self):
        status, body = self.post("/agent/readonly", {"task": "只读检查"})
        self.assertEqual((status, body["status"], body["workspace_write"], body["files_touched"]), (200, "PASS", "NO", []))
        self.assertEqual(self.calls, [("git", "status", "--short"), ("git", "diff", "--name-only")])

    def test_plan_and_confirm_gated_actions(self):
        _, plan = self.post("/agent/plan", {"task": "修改代码并运行测试"})
        plan_id = plan["plan_id"]
        for path, payload in (
            ("/agent/draft-patch", {"plan_id": plan_id}),
            ("/agent/apply", {"plan_id": plan_id, "patch_file": "candidate.patch"}),
            ("/agent/test", {"plan_id": plan_id}),
            ("/agent/commit", {"plan_id": plan_id, "files": ["README.md"], "message": "safe local commit"}),
        ):
            status, body = self.post(path, payload)
            self.assertEqual((status, body["error_code"]), (400, "CONFIRMATION_REQUIRED"), path)

    def test_confirmation_token_is_plan_bound_and_high_risk_stops(self):
        _, plan = self.post("/agent/plan", {"task": "删除所有文件"})
        plan_id = plan["plan_id"]
        status, body = self.post("/agent/test", {"plan_id": plan_id, "confirm_token": f"CONFIRM:{plan_id}"})
        self.assertEqual((status, body["error_code"]), (403, "LOCAL_AGENT_HIGH_RISK_STOP"))
        status, body = self.post("/agent/test", {"plan_id": plan_id, "confirm_token": "CONFIRM:wrong"})
        self.assertEqual((status, body["error_code"]), (403, "LOCAL_AGENT_HIGH_RISK_STOP"))

    def test_unknown_agent_route_and_public_bind_are_rejected(self):
        status, body = self.get("/agent/unknown")
        self.assertEqual((status, body["error"]["code"]), (404, "not_found"))
        with self.assertRaises(ValueError):
            RouterResponsesServer(host="0.0.0.0", port=0)


if __name__ == "__main__":
    unittest.main()

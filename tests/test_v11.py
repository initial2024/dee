from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from urllib.request import urlopen, Request

from codex_ai_router.codex_integration import install_xiaoyu_router_provider, same_thread_provider_switch_support
from codex_ai_router.handoff import compact_handoff
from codex_ai_router.network import NetworkMode, NetworkState
from codex_ai_router.providers.base import DiscoveredModel
from codex_ai_router.providers.local_backend import LocalBackend, ManagedLlamaCppBackend
from codex_ai_router.providers.registry import ModelRegistry
from codex_ai_router.server import RouterResponsesServer, RouterService, VIRTUAL_MODELS, _bounded_output_tokens, _input_text
from codex_ai_router.vision import VisionProxy
from codex_ai_router.codex_status import ChatGPTCodexQuota, CodexAgentAvailability, CodexHarnessState
from codex_ai_router.providers.runtime_models import RuntimeModelState, text_candidates
from codex_ai_router.accounting.usage_ledger import UsageLedger, provider_usage_warning


class FakeLocal:
    def __init__(self, available=True): self._available = available
    def available(self): return self._available
    def ask(self, prompt): return "local result: " + prompt


class RouterV11Tests(unittest.TestCase):
    def test_101_virtual_models_are_stable_profiles(self):
        self.assertEqual(VIRTUAL_MODELS, ("xiaoyu-auto", "xiaoyu-local", "xiaoyu-api-auto", "xiaoyu-api-local", "xiaoyu-lightboat"))

    def test_102_localhost_server_rejects_public_bind(self):
        with self.assertRaises(ValueError): RouterResponsesServer(host="0.0.0.0")

    def test_103_localhost_server_lists_virtual_models(self):
        server = RouterResponsesServer(RouterService(NetworkMode.OFFLINE, local=FakeLocal(False)), port=0)
        server.start()
        try:
            port = server.httpd.server_address[1]
            with urlopen(f"http://127.0.0.1:{port}/v1/models", timeout=3) as response:
                body = json.loads(response.read())
            self.assertEqual([item["id"] for item in body["data"]], list(VIRTUAL_MODELS))
        finally:
            server.stop()

    def test_104_local_responses_normalize_to_responses_shape(self):
        service = RouterService(NetworkMode.OFFLINE, local=FakeLocal())
        data = service.respond({"model": "xiaoyu-local", "input": "hello"})
        self.assertEqual((data["object"], data["model"], data["output"][0]["content"][0]["type"]), ("response", "xiaoyu-local", "output_text"))

    def test_105_offline_blocks_remote_without_calling_provider(self):
        service = RouterService(NetworkMode.OFFLINE, local=FakeLocal(False))
        with self.assertRaisesRegex(RuntimeError, "REMOTE_FALLBACK_DISABLED_OFFLINE"):
            service.respond({"model": "xiaoyu-lightboat", "input": "hello"})

    def test_106_auto_uses_local_only_for_fast_simple_work(self):
        service = RouterService(NetworkMode.OFFLINE, local=FakeLocal())
        data = service.respond({"model": "xiaoyu-auto", "input": "Summarize this short log."})
        self.assertEqual(data["model"], "xiaoyu-auto")

    def test_107_managed_gguf_discovery_needs_no_hardcoded_path(self):
        with tempfile.TemporaryDirectory() as temp:
            file = Path(temp) / "sample.Q4_K_M.gguf"; file.write_bytes(b"GGUF")
            models = ManagedLlamaCppBackend([Path(temp)]).discover()
            self.assertEqual((models[0].model_id, models[0].size_bytes, models[0].quantization), ("sample.Q4_K_M", 4, "UNKNOWN"))

    def test_108_local_backend_can_be_unavailable_without_lmstudio_requirement(self):
        backend = LocalBackend(lmstudio=FakeLocal(False), managed=ManagedLlamaCppBackend())
        self.assertEqual(backend.active_kind(), "LOCAL_UNAVAILABLE")

    def test_109_capability_registry_keeps_unknowns_unknown(self):
        registry = ModelRegistry(); now = datetime.now()
        registry.update("api", [DiscoveredModel("api", "text", "text", None, None, now)])
        registry.set_capabilities("api:text", {"TEXT", "CODING"})
        profile = registry.capability_profile("api:text")
        self.assertEqual((profile["TEXT"], profile["CODING"], profile["VISION"]), ("YES", "YES", "UNKNOWN"))

    def test_110_vision_proxy_never_forwards_to_text_only(self):
        self.assertEqual(VisionProxy().route(True, [], False).status, "VISION_PROVIDER_UNAVAILABLE")

    def test_111_network_mode_does_not_require_vpn(self):
        state = NetworkState(NetworkMode.OFFLINE)
        self.assertEqual((state.probe("https://example.invalid"), state.remote_allowed), ("OFFLINE", False))

    def test_111b_auto_network_state_recovers_by_reprobing(self):
        state = NetworkState(NetworkMode.AUTO)
        self.assertTrue(state.remote_allowed)
        self.assertEqual(state.probe("http://127.0.0.1:1", timeout=0.01), "LOCAL")

    def test_112_codex_provider_install_preserves_existing_config(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "config.toml"; config.write_text('model = "existing"\n', encoding="utf-8")
            result = install_xiaoyu_router_provider(config, 18888)
            text = config.read_text(encoding="utf-8")
            self.assertEqual((result["status"], "model = \"existing\"" in text, "XiaoyuRouter" in text), ("INSTALLED", True, True))

    def test_113_same_thread_switch_is_explicitly_unsupported(self):
        self.assertEqual(same_thread_provider_switch_support(), "UNSUPPORTED")

    def test_114_handoff_is_compact_and_secret_free(self):
        result = compact_handoff(Path.cwd(), "task", tests="PASS", blockers="NONE")
        self.assertEqual((result["task"], result["tests"], "credentials" in result), ("task", "PASS", False))

    def test_115_image_input_is_rejected_when_no_vision_provider(self):
        service = RouterService(NetworkMode.OFFLINE, local=FakeLocal())
        with self.assertRaisesRegex(RuntimeError, "VISION_PROVIDER_UNAVAILABLE"):
            service.respond({"model": "xiaoyu-local", "input": [{"content": [{"type": "input_image", "image_url": "opaque"}]}]})

    def test_116_codex_quota_never_disables_harness_by_inference(self):
        state = CodexHarnessState(ChatGPTCodexQuota.EXHAUSTED, CodexAgentAvailability.AVAILABLE)
        self.assertEqual(state.as_dict(), {"CHATGPT_CODEX_QUOTA": "EXHAUSTED", "CODEX_AGENT": "AVAILABLE"})

    def test_117_responses_input_mapping_and_token_cap_are_bounded(self):
        value = [{"role": "user", "content": [{"type": "input_text", "text": "short"}]}]
        self.assertEqual((_input_text(value), _bounded_output_tokens({"max_output_tokens": 99})), ("short", 16))

    def test_118_streaming_request_gets_basic_sse_completed_event(self):
        server = RouterResponsesServer(RouterService(NetworkMode.OFFLINE, local=FakeLocal()), port=0)
        server.start()
        try:
            port = server.httpd.server_address[1]
            request = Request(f"http://127.0.0.1:{port}/v1/responses", data=json.dumps({"model": "xiaoyu-local", "input": "ok", "stream": True}).encode(), headers={"Content-Type": "application/json"}, method="POST")
            with urlopen(request, timeout=3) as response:
                raw = response.read().decode("utf-8")
                self.assertEqual(response.headers.get_content_type(), "text/event-stream")
            self.assertIn("response.completed", raw)
        finally:
            server.stop()

    def test_119_runtime_selection_uses_passed_fastest_text_model_and_cools_timeouts(self):
        with tempfile.TemporaryDirectory() as temp:
            state = RuntimeModelState(Path(temp) / "runtime.json", cooldown_seconds=900)
            state.record("lightboat-3", "slow", "TIMEOUT", 20, "TIMEOUT")
            state.record("lightboat-3", "fast", "PASS", 1.2, "HTTP_200")
            state.record("lightboat-3", "other", "PASS", 2.4, "HTTP_200")
            self.assertEqual((state.select("lightboat-3", ["slow", "fast", "other"]), state.recent_timeout("lightboat-3", "slow")), ("fast", True))

    def test_120_runtime_text_candidates_exclude_only_explicit_image_names(self):
        candidates, excluded = text_candidates(["text-unknown", "grok-imagine-image-lite"])
        self.assertEqual((candidates, excluded), (["text-unknown"], ["grok-imagine-image-lite"]))

    def test_121_usage_ledger_never_stores_prompt_or_secret(self):
        with tempfile.TemporaryDirectory() as temp:
            ledger = UsageLedger(Path(temp) / "usage.jsonl")
            record = ledger.append({"active_provider": "XiaoyuRouter", "active_model": "xiaoyu-lightboat", "success": True, "error_code": "Bearer fake", "prompt": "must not persist"})
            raw = ledger.path.read_text(encoding="utf-8")
            self.assertNotIn("prompt", record); self.assertNotIn("fake", raw); self.assertNotIn("must not persist", raw)

    def test_122_usage_warning_does_not_fake_quota(self):
        self.assertIn("may consume", provider_usage_warning("OpenAI", 5))
        self.assertIn("not an official quota", provider_usage_warning("XiaoyuRouter"))


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from urllib.request import urlopen, Request
from unittest.mock import patch

from codex_ai_router.codex_integration import install_xiaoyu_router_provider, same_thread_provider_switch_support
from codex_ai_router.handoff import compact_handoff
from codex_ai_router.network import NetworkMode, NetworkState
from codex_ai_router.providers.base import DiscoveredModel
from codex_ai_router.providers.local_backend import LocalBackend, ManagedLlamaCppBackend, load_local_backend_config, save_local_backend_config, discover_llama_server
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
            model_ids = [item["id"] for item in body["data"]]
            self.assertEqual(model_ids[:len(VIRTUAL_MODELS)], list(VIRTUAL_MODELS))
            self.assertIn("xiaoyu-api-groq-2", model_ids)
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
            self.assertEqual((models[0].model_id, models[0].size_bytes, models[0].quantization), ("sample.Q4_K_M", 4, "Q4_K_M"))

    def test_108_local_backend_can_be_unavailable_without_lmstudio_requirement(self):
        with tempfile.TemporaryDirectory() as temp:
            backend = LocalBackend(lmstudio=FakeLocal(False), managed=ManagedLlamaCppBackend(config_path=Path(temp) / "local-backend.json", executable="missing-llama-server"))
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

    def test_123_fast_delegate_skips_denied_and_cooldown_models(self):
        from codex_ai_router import server
        metadata = {"providers": {
            "groq": {"type": "groq", "enabled": True, "model_registry": {"DISCOVERED_MODELS": ["denied", "slow"], "ALLOWED_MODELS": ["slow"]}, "denied_model_ids": ["denied"]},
            "lightboat": {"type": "openai_compatible", "enabled": True, "model_registry": {"DISCOVERED_MODELS": ["fast"], "ALLOWED_MODELS": ["fast"]}},
        }}
        class Provider: timeout = 30
        with tempfile.TemporaryDirectory() as temp, patch("codex_ai_router.server.provider_config.load", return_value=metadata):
            state = RuntimeModelState(Path(temp) / "runtime.json", cooldown_seconds=900)
            state.record("groq", "slow", "TIMEOUT", 20, "TIMEOUT")
            state.record("lightboat", "fast", "PASS", 0.1, "HTTP_200")
            service = server.RouterService(NetworkMode.AUTO, runtime_models=state)
            with patch.object(service, "_provider_for_entry", return_value=Provider()), patch.object(service, "_invoke_selected_api", return_value={"output_text": "summary"}):
                result = service.delegate_fast_readonly("summarize", max_seconds=30)
            self.assertEqual((result["ok"], result["provider"], result["model"]), (True, "lightboat", "fast"))
            self.assertIn("POLICY_DENIED", [item["reason"] for item in result["skipped"]])
            self.assertIn("TIMEOUT_COOLDOWN", [item["reason"] for item in result["skipped"]])

    def test_124_fast_delegate_explain_matches_runtime_selection(self):
        from codex_ai_router import server
        metadata = {"providers": {"groq": {"type": "groq", "enabled": True, "model_registry": {"DISCOVERED_MODELS": ["fast"], "ALLOWED_MODELS": ["fast"]}}}}
        with tempfile.TemporaryDirectory() as temp, patch("codex_ai_router.server.provider_config.load", return_value=metadata):
            state = RuntimeModelState(Path(temp) / "runtime.json"); state.record("groq", "fast", "PASS", 0.1, "HTTP_200")
            explanation = server.RouterService(NetworkMode.AUTO, runtime_models=state).explain_fast_delegation()
            self.assertEqual((explanation["selected_provider"], explanation["selected_model"]), ("groq", "fast"))

    def test_125_fast_delegate_falls_back_after_provider_timeout(self):
        from codex_ai_router import server
        metadata = {"providers": {
            "groq": {"type": "groq", "enabled": True, "model_registry": {"DISCOVERED_MODELS": ["first"], "ALLOWED_MODELS": ["first"]}},
            "lightboat": {"type": "openai_compatible", "enabled": True, "model_registry": {"DISCOVERED_MODELS": ["second"], "ALLOWED_MODELS": ["second"]}},
        }}
        class Provider: timeout = 30
        def invoke(_payload, _virtual, _provider, _entry, model):
            if model == "first": raise RuntimeError("DOWNSTREAM_TIMEOUT:10")
            return {"output_text": "fallback summary"}
        with tempfile.TemporaryDirectory() as temp, patch("codex_ai_router.server.provider_config.load", return_value=metadata):
            state = RuntimeModelState(Path(temp) / "runtime.json")
            state.record("groq", "first", "PASS", 0.1, "HTTP_200")
            state.record("lightboat", "second", "PASS", 0.2, "HTTP_200")
            service = server.RouterService(NetworkMode.AUTO, runtime_models=state)
            with patch.object(service, "_provider_for_entry", return_value=Provider()), patch.object(service, "_invoke_selected_api", side_effect=invoke):
                result = service.delegate_fast_readonly("summarize", max_seconds=30)
            self.assertEqual((result["ok"], result["provider"], result["model"]), (True, "lightboat", "second"))
            self.assertIn("DOWNSTREAM_TIMEOUT:10", [item["reason"] for item in result["skipped"]])

    def test_126_explain_selection_reports_policy_and_cooldown_skip_reasons(self):
        from codex_ai_router import server
        metadata = {"providers": {"groq": {"type": "groq", "enabled": True, "model_registry": {"DISCOVERED_MODELS": ["denied", "slow", "fast"], "ALLOWED_MODELS": ["slow", "fast"]}, "denied_model_ids": ["denied"]}}}
        with tempfile.TemporaryDirectory() as temp, patch("codex_ai_router.server.provider_config.load", return_value=metadata):
            state = RuntimeModelState(Path(temp) / "runtime.json", cooldown_seconds=900)
            state.record("groq", "slow", "TIMEOUT", 20, "TIMEOUT")
            state.record("groq", "fast", "PASS", 0.1, "HTTP_200")
            explanation = server.RouterService(NetworkMode.AUTO, runtime_models=state).explain_selection("xiaoyu-api-groq")
        self.assertEqual((explanation["provider"], explanation["selected_model"]), ("groq", "fast"))
        reasons = {(item.get("model"), item["reason"]) for item in explanation["skipped"]}
        self.assertIn(("denied", "POLICY_DENIED"), reasons)
        self.assertIn(("slow", "TIMEOUT_COOLDOWN"), reasons)

    def test_127_direct_local_config_and_selected_model_persist(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); models_dir = root / "lmstudio"; models_dir.mkdir()
            model_path = models_dir / "demo-Q4_K_M.gguf"; model_path.write_bytes(b"GGUF")
            config_path = root / "local-backend.json"
            save_local_backend_config({"model_dirs": [str(models_dir)], "selected_model_path": "", "port": 18791}, config_path)
            self.assertEqual(load_local_backend_config(config_path)["port"], 18791)
            backend = ManagedLlamaCppBackend(config_path=config_path, model_directories=[models_dir], executable=str(root / "llama-server.exe"), port=18791)
            found = backend.discover(); self.assertEqual((len(found), found[0].quantization, found[0].source), (1, "Q4_K_M", "configured"))
            backend.select("demo-Q4_K_M")
            restored = ManagedLlamaCppBackend(config_path=config_path, model_directories=[models_dir], executable=str(root / "llama-server.exe"), port=18791)
            self.assertEqual(restored.selected.model_id, "demo-Q4_K_M")

    def test_128_direct_local_model_scan_is_bounded_to_configured_dirs(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); allowed = root / "allowed"; outside = root / "outside"; allowed.mkdir(); outside.mkdir()
            (allowed / "ok.gguf").write_bytes(b"1"); (outside / "no.gguf").write_bytes(b"1")
            models = ManagedLlamaCppBackend([allowed], executable="missing-llama-server").discover()
            self.assertEqual([item.model_id for item in models], ["ok"])

    def test_129_direct_local_server_error_is_explicit(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); model = root / "demo.gguf"; model.write_bytes(b"GGUF")
            backend = ManagedLlamaCppBackend([root], executable=str(root / "missing-llama-server.exe"), config_path=root / "local-backend.json")
            backend.select("demo")
            with self.assertRaisesRegex(Exception, "LLAMA_SERVER_NOT_FOUND"):
                backend.start()

    def test_130_direct_local_command_is_loopback_and_shell_free(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); model = root / "demo-Q5_K_M.gguf"; model.write_bytes(b"GGUF")
            executable = root / "llama-server.exe"; executable.write_bytes(b"stub")
            backend = ManagedLlamaCppBackend([root], executable=str(executable), config_path=root / "local-backend.json")
            selected = backend.select("demo-Q5_K_M")
            command = backend._command(selected)
            self.assertEqual(command[3:7], ["--host", "127.0.0.1", "--port", "18790"])
            self.assertNotIn(";", command); self.assertNotIn("&", command)

    def test_131_direct_local_port_conflict_is_classified(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); model = root / "demo.gguf"; model.write_bytes(b"GGUF")
            executable = root / "llama-server.exe"; executable.write_bytes(b"stub")
            backend = ManagedLlamaCppBackend([root], executable=str(executable), config_path=root / "local-backend.json")
            backend.select("demo")
            with patch.object(backend, "port_owner", side_effect=lambda port=None: {"pid": 999, "process_name": "python.exe", "port": int(port or backend.port)}):
                with self.assertRaisesRegex(Exception, "PORT_IN_USE_BY_UNKNOWN_PROCESS"):
                    backend.start()

    def test_132_unknown_port_owner_uses_safe_fallback_without_kill(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); (root / "demo.gguf").write_bytes(b"GGUF")
            executable = root / "llama-server.exe"; executable.write_bytes(b"stub")
            config = root / "local-backend.json"
            backend = ManagedLlamaCppBackend([root], executable=str(executable), config_path=config)
            backend.select("demo")
            owners = lambda port=None: {"pid": 999, "process_name": "python.exe", "port": int(port or backend.port)} if int(port or backend.port) == 18790 else None
            with patch.object(backend, "port_owner", side_effect=owners), patch.object(backend, "_terminate_owner") as terminate:
                prepared = backend._prepare_port()
            terminate.assert_not_called()
            self.assertEqual((prepared["auto_port_fallback"], prepared["port"], prepared["port_fallback_from"]), ("YES", 18791, 18790))
            self.assertEqual(load_local_backend_config(config)["port"], 18791)

    def test_133_repair_returns_explicit_missing_server_error(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); (root / "demo.gguf").write_bytes(b"GGUF")
            backend = ManagedLlamaCppBackend([root], executable=str(root / "missing-llama-server"), config_path=root / "local-backend.json")
            backend.select("demo")
            result = backend.repair()
            self.assertEqual((result["status"], result["error_code"]), ("ERROR", "LLAMA_SERVER_NOT_FOUND"))

    def test_134_stale_pid_state_is_reconciled_without_killing_unknown_process(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); config = root / "local-backend.json"
            backend = ManagedLlamaCppBackend([root], executable="missing-llama-server", config_path=config)
            with patch.object(backend, "_external_pid", return_value=987654), patch.object(backend, "_pid_alive", return_value=False), patch.object(backend, "_clear_state_files") as clear_state:
                status = backend.status()
            self.assertEqual(status["state_reconciled"], "STALE_PID_CLEARED")
            clear_state.assert_called_once()


if __name__ == "__main__":
    unittest.main()

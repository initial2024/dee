from __future__ import annotations

import tempfile
import unittest
from unittest import mock
from pathlib import Path

from codex_ai_router.providers.local_backend import (
    BONSAI_MINIMAL_SMOKE,
    LOCAL_PERFORMANCE_PROFILES,
    ProviderError,
    PRISM_RUNTIME_ID,
    STANDARD_RUNTIME_ID,
    VULKAN_RUNTIME_ID,
    GGUFModel,
    ManagedLlamaCppBackend,
    discover_gguf_models,
    classify_bonsai_performance,
    configure_pr206_avx2_variant,
    load_local_backend_config,
    prism_bonsai_runtime_status,
    prism_bonsai_acceleration_status,
    runtime_for_model,
    default_local_backend_config,
    save_local_backend_config,
)
from codex_ai_router.providers.local_model_selector import LocalModelSelector, bonsai_format_from_filename, profile_from_model


class LocalSelectorTests(unittest.TestCase):
    def profiles(self):
        root = Path(tempfile.mkdtemp())
        models = [
            GGUFModel(root / "Qwen2.5-7B-Instruct-Q4_K_M.gguf", "qwen", 1, quantization="Q4_K_M"),
            GGUFModel(root / "L3-8B-Stheno-Q4_K_S.gguf", "stheno", 1, quantization="Q4_K_S"),
            GGUFModel(root / "Qwen3-14B-Q4_K_M.gguf", "big", 1, quantization="Q4_K_M"),
            GGUFModel(root / "mmproj-Qwen3-BF16.gguf", "mmproj", 1, quantization="F16", vision_projector=True, text_model=False),
            GGUFModel(root / "heavy-7B-BF16.gguf", "bf16", 1, quantization="BF16"),
        ]
        return [profile_from_model(item) for item in models]

    def test_profiles_exclude_mmproj_as_text(self):
        profile = next(item for item in self.profiles() if item["model_id"] == "mmproj")
        self.assertEqual(profile["text_model"], "NO")
        self.assertEqual(profile["vision_projector"], "YES")

    def test_simple_task_selects_instruct_and_skips_heavy(self):
        result = LocalModelSelector(self.profiles()).select("解释这个 Python 报错，不修改文件")
        self.assertEqual(result["selected_model"], "qwen")
        reasons = {item["reason"] for item in result["skipped_models"]}
        self.assertIn("MMPROJ_NOT_TEXT_MODEL", reasons)
        self.assertIn("BF16_AUTO_DISABLED", reasons)
        self.assertIn("HEAVY_MODEL_REQUIRES_HIGH_THRESHOLD", reasons)

    def test_roleplay_selects_creative(self):
        result = LocalModelSelector(self.profiles()).select("写一段角色扮演开场", mode="roleplay")
        self.assertEqual(result["selected_model"], "stheno")

    def test_complex_and_high_risk_stop_or_require_escalation(self):
        complex_result = LocalModelSelector(self.profiles()).select("复杂多文件重构", risk="complex")
        self.assertTrue(complex_result["requires_api_or_official_codex"])
        high_result = LocalModelSelector(self.profiles()).select("production deploy", risk="high")
        self.assertEqual(high_result["error_code"], "LOCAL_HIGH_RISK_SAFE_STOP")

    def test_manual_policy_priority(self):
        profiles = self.profiles()
        result = LocalModelSelector(profiles, policy={"manual_disabled_models": ["qwen"], "manual_only_model": "stheno"}).select("解释错误")
        self.assertEqual(result["selected_model"], "stheno")
        denied = LocalModelSelector(profiles, policy={"manual_disabled_models": ["stheno"], "manual_only_model": "stheno"}).select("解释错误")
        self.assertIsNone(denied["selected_model"])

    def test_config_discovery_marks_mmproj(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "mmproj-Qwen-BF16.gguf").write_bytes(b"x")
            found = discover_gguf_models([root])
            self.assertEqual(found[0].text_model, False)
            self.assertEqual(found[0].vision_projector, True)

    def test_bonsai_filename_formats_are_profiled_and_not_auto_selected(self):
        root = Path(tempfile.mkdtemp())
        bonsai = GGUFModel(root / "Ternary-Bonsai-7B-Instruct-PTQ1_0.gguf", "bonsai", 1, quantization="UNKNOWN")
        qwen = GGUFModel(root / "Qwen2.5-7B-Instruct-Q4_K_M.gguf", "qwen", 1, quantization="Q4_K_M")
        profile = profile_from_model(bonsai)
        self.assertEqual((profile["family_guess"], profile["bonsai_model"], profile["bonsai_format"], profile["quantization"]), ("Ternary-Bonsai", "YES", "PTQ1_0", "PTQ1_0"))
        result = LocalModelSelector([profile, profile_from_model(qwen)]).select("解释这个错误")
        self.assertEqual(result["selected_model"], "qwen")
        self.assertIn({"model": "bonsai", "reason": "BONSAI_REQUIRES_EXPLICIT_COMPATIBILITY_CHECK"}, result["skipped_models"])

    def test_all_reserved_bonsai_filename_formats_are_recognized(self):
        cases = {
            "Bonsai-Q2_0_g64.gguf": "Q2_0_G64",
            "Ternary-Bonsai-PQ2_0.gguf": "PQ2_0",
            "Ternary-Bonsai-PTQ1_0.gguf": "PTQ1_0",
            "Bonsai-Q1_0.gguf": "Q1_0",
        }
        for filename, expected in cases.items():
            self.assertEqual(bonsai_format_from_filename(filename), expected)

    def test_runtime_routing_requires_prism_for_bonsai_and_preserves_standard(self):
        root = Path(tempfile.mkdtemp())
        standard = GGUFModel(root / "Qwen-Q4_K_M.gguf", "standard", 1, quantization="Q4_K_M")
        ternary = GGUFModel(root / "Ternary-Bonsai-PQ2_0.gguf", "ternary", 1, quantization="PQ2_0")
        projector = GGUFModel(root / "mmproj-Bonsai.gguf", "projector", 1, quantization="UNKNOWN", text_model=False, vision_projector=True)
        self.assertEqual(runtime_for_model(standard)[0], STANDARD_RUNTIME_ID)
        self.assertEqual(runtime_for_model(ternary)[0], PRISM_RUNTIME_ID)
        self.assertEqual(runtime_for_model(projector), (None, "MMPROJ_NOT_TEXT_MODEL"))

    def test_uninstalled_prism_runtime_is_unknown_and_never_ready_for_download(self):
        root = Path(tempfile.mkdtemp()) / "prism-bonsai-runtime"
        status = prism_bonsai_runtime_status({"runtimes": {PRISM_RUNTIME_ID: {"runtime_path": str(root)}}})
        self.assertEqual(status["runtime_installed"], "NO")
        self.assertEqual(status["compatibility_status"], "BONSAI_RUNTIME_UNKNOWN")
        self.assertEqual(status["NO_IMPLICIT_RUNTIME_BUILD"], "YES")

    def test_bonsai_minimal_smoke_is_bounded_and_never_auto_enabled(self):
        self.assertLessEqual(BONSAI_MINIMAL_SMOKE["max_tokens"], 8)
        self.assertEqual(BONSAI_MINIMAL_SMOKE["temperature"], 0)
        classification, policy = classify_bonsai_performance("PASS", 56.223)
        self.assertEqual(classification, "BONSAI_USABLE_FAST")
        self.assertIn("Manual", policy)

    def test_bonsai_cpu_fallback_has_its_own_measured_safe_defaults(self):
        config = default_local_backend_config()
        self.assertEqual(config["bonsai_ctx_size"], 1024)
        self.assertEqual(config["bonsai_cpu_threads"], 12)
        with tempfile.TemporaryDirectory() as temp:
            status = prism_bonsai_acceleration_status({"runtimes": {PRISM_RUNTIME_ID: {"runtime_path": temp}}})
        self.assertEqual(status["PQ2_0_GPU_BACKEND_FOUND"], "NO")
        self.assertEqual(status["PQ2_0_CPU_FALLBACK_ACTIVE"], "YES")
        self.assertEqual(status["bonsai_acceleration_available"], "NO")
        self.assertEqual(status["bonsai_gpu_offload"], "0")
        self.assertEqual(status["bonsai_runtime_variant"], "stable_prism_cpu")
        self.assertEqual(status["bonsai_mtp_enabled"], "NO")

    def test_prism_hip_candidate_requires_an_enumerated_device_before_acceleration(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "llama-server.exe").write_bytes(b"")
            (root / "ggml-hip.dll").write_bytes(b"")
            completed = mock.Mock(stdout="Available devices:\n  HIP0: AMD test device\n", stderr="")
            with mock.patch("codex_ai_router.providers.local_backend.subprocess.run", return_value=completed):
                status = prism_bonsai_acceleration_status({"runtimes": {PRISM_RUNTIME_ID: {"runtime_path": temp}}})
        self.assertEqual(status["bonsai_acceleration_available"], "YES")
        self.assertEqual(status["bonsai_acceleration_backend"], "HIP")
        self.assertEqual(status["PQ2_0_CPU_FALLBACK_ACTIVE"], "NO")

    def test_bonsai_timeout_is_classified_without_auto_selection(self):
        classification, policy = classify_bonsai_performance("TIMEOUT", 300, "COMPLETION_TIMEOUT")
        self.assertEqual(classification, "BONSAI_LOADS_BUT_TOO_SLOW")
        self.assertIn("automatic selection", policy)

    def test_warm_model_and_measured_speed_affect_local_routing(self):
        profiles = self.profiles()
        qwen = next(item for item in profiles if item["model_id"] == "qwen")
        qwen["measured_generation_tps"] = 0.919
        result = LocalModelSelector(profiles, current_model="stheno").select("解释这个错误")
        self.assertEqual(result["selected_model"], "stheno")
        self.assertEqual(result["keep_warm_model"], "YES")
        self.assertEqual(result["model_switch_cost_accounted"], "YES")

    def test_vulkan_preference_defaults_to_safe_auto_fallback(self):
        config = default_local_backend_config()
        self.assertEqual(config["backend_preference"], "AUTO")
        self.assertEqual(LOCAL_PERFORMANCE_PROFILES["fast"]["ctx_size"], 2048)

    def test_standard_runtime_selection_sanitizes_preferences_and_requires_vulkan_availability(self):
        with tempfile.TemporaryDirectory() as temp:
            config_path = Path(temp) / "local-backend.json"
            backend = ManagedLlamaCppBackend(config_path=config_path)
            cases = (
                ("CPU", {"runtime_installed": "YES", "recommended_backend": "VULKAN"}, STANDARD_RUNTIME_ID),
                ("VULKAN", {"runtime_installed": "YES", "recommended_backend": "CPU"}, VULKAN_RUNTIME_ID),
                ("VULKAN", {"runtime_installed": "NO", "recommended_backend": "VULKAN"}, STANDARD_RUNTIME_ID),
                ("AUTO", {"runtime_installed": "YES", "recommended_backend": "VULKAN"}, VULKAN_RUNTIME_ID),
                ("AUTO", {"runtime_installed": "YES", "recommended_backend": "CPU"}, STANDARD_RUNTIME_ID),
                ("INVALID", {"runtime_installed": "NO", "recommended_backend": "VULKAN"}, STANDARD_RUNTIME_ID),
            )
            for preference, status, expected in cases:
                with self.subTest(preference=preference, status=status):
                    config = default_local_backend_config()
                    config["backend_preference"] = preference
                    save_local_backend_config(config, config_path)
                    with mock.patch.object(backend, "vulkan_status", return_value=status):
                        self.assertEqual(backend.preferred_standard_runtime(), expected)

    def test_pr206_configuration_validates_external_binary_and_uses_temp_config(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            executable = root / "llama-server.exe"
            executable.write_bytes(b"test-only-pr206")
            config_path = root / "local-backend.json"
            config = default_local_backend_config()
            with mock.patch("codex_ai_router.providers.local_backend._runtime_probe_output", return_value=("10df2988 test", "")):
                variant = configure_pr206_avx2_variant(config, executable)
            self.assertEqual(variant["expected_commit"][:8], "10df2988")
            save_local_backend_config(config, config_path)
            persisted = load_local_backend_config(config_path)
            self.assertEqual(persisted["runtimes"][PRISM_RUNTIME_ID]["variants"]["experimental_pr206_avx2"]["llama_server_path"], str(executable.resolve()))
            with self.assertRaisesRegex(ProviderError, "PR206_RUNTIME_COMMIT_MISMATCH"):
                configure_pr206_avx2_variant(default_local_backend_config(), root / "missing.exe")

    def test_pr206_configuration_rejects_empty_path_without_writing_config(self):
        config: dict = {}
        with self.assertRaisesRegex(ProviderError, "PR206_RUNTIME_COMMIT_MISMATCH"):
            configure_pr206_avx2_variant(config, "")
        self.assertEqual(config, {})

    def test_pr206_configuration_builds_missing_runtime_structure_in_temp_config(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            executable = root / "llama-server.exe"
            executable.write_bytes(b"test-only-pr206")
            config_path = root / "local-backend.json"
            config: dict = {}
            with mock.patch("codex_ai_router.providers.local_backend._runtime_probe_output", return_value=("10df2988 test", "")):
                configure_pr206_avx2_variant(config, executable)
            save_local_backend_config(config, config_path)
            persisted = load_local_backend_config(config_path)
            self.assertEqual(persisted["runtimes"][PRISM_RUNTIME_ID]["variants"]["experimental_pr206_avx2"]["llama_server_path"], str(executable.resolve()))

    def test_pr206_configuration_rejects_nonmatching_existing_binary_without_writing_config(self):
        with tempfile.TemporaryDirectory() as temp:
            executable = Path(temp) / "llama-server.exe"
            executable.write_bytes(b"test-only-nonmatching")
            config: dict = {}
            with mock.patch("codex_ai_router.providers.local_backend._runtime_probe_output", return_value=("different build", "")):
                with self.assertRaisesRegex(ProviderError, "PR206_RUNTIME_COMMIT_MISMATCH"):
                    configure_pr206_avx2_variant(config, executable)
            self.assertEqual(config, {})

    def test_vulkan_start_uses_one_bounded_cpu_fallback_without_real_process(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            model_path = root / "demo-Q4_K_M.gguf"
            model_path.write_bytes(b"GGUF")
            cpu_executable = root / "llama-server.exe"
            cpu_executable.write_bytes(b"test-only-cpu")
            config_path = root / "local-backend.json"
            config = default_local_backend_config()
            config["llama_server_path"] = str(cpu_executable)
            save_local_backend_config(config, config_path)
            backend = ManagedLlamaCppBackend(config_path=config_path, executable=str(cpu_executable))
            model = GGUFModel(model_path, "demo", model_path.stat().st_size, quantization="Q4_K_M")
            fake_process = mock.Mock(pid=12345)
            commands: list[str] = []

            def fake_command(_model, runtime_id, prefer_experimental=True):
                commands.append(runtime_id)
                return ["vulkan-test"] if runtime_id == VULKAN_RUNTIME_ID else ["cpu-test"]

            with (
                mock.patch.object(backend, "preferred_standard_runtime", return_value=VULKAN_RUNTIME_ID),
                mock.patch.object(backend, "running", return_value=False),
                mock.patch.object(backend, "_prepare_port"),
                mock.patch.object(backend, "_command", side_effect=fake_command),
                mock.patch.object(backend, "executable_available", return_value=True),
                mock.patch.object(backend, "wait_ready", side_effect=[False, True]),
                mock.patch.object(backend, "stop"),
                mock.patch.object(backend, "status", return_value={"status": "PASS"}),
                mock.patch("codex_ai_router.providers.local_backend.subprocess.Popen", return_value=fake_process) as popen,
            ):
                self.assertEqual(backend.start(model), {"status": "PASS"})
            self.assertEqual(commands, [VULKAN_RUNTIME_ID, STANDARD_RUNTIME_ID])
            self.assertEqual(popen.call_count, 2)
            self.assertEqual(popen.call_args_list[1].args[0], ["cpu-test"])
            self.assertIn("VULKAN_START_OR_LOAD_FAILED", backend.state_path.read_text(encoding="utf-8"))

    def test_vulkan_and_cpu_candidate_failures_stop_after_one_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            model_path = root / "demo-Q4_K_M.gguf"
            model_path.write_bytes(b"GGUF")
            cpu_executable = root / "llama-server.exe"
            cpu_executable.write_bytes(b"test-only-cpu")
            config_path = root / "local-backend.json"
            config = default_local_backend_config()
            config["llama_server_path"] = str(cpu_executable)
            save_local_backend_config(config, config_path)
            backend = ManagedLlamaCppBackend(config_path=config_path, executable=str(cpu_executable))
            model = GGUFModel(model_path, "demo", model_path.stat().st_size, quantization="Q4_K_M")
            fake_process = mock.Mock(pid=12346)
            with (
                mock.patch.object(backend, "preferred_standard_runtime", return_value=VULKAN_RUNTIME_ID),
                mock.patch.object(backend, "running", return_value=False),
                mock.patch.object(backend, "_prepare_port"),
                mock.patch.object(backend, "_command", side_effect=lambda _model, runtime_id, prefer_experimental=True: [runtime_id]),
                mock.patch.object(backend, "executable_available", return_value=True),
                mock.patch.object(backend, "wait_ready", side_effect=[False, False]),
                mock.patch.object(backend, "stop"),
                mock.patch("codex_ai_router.providers.local_backend.subprocess.Popen", return_value=fake_process) as popen,
            ):
                with self.assertRaisesRegex(ProviderError, "HEALTH_TIMEOUT"):
                    backend.start(model)
            self.assertEqual(popen.call_count, 2)

    def test_vulkan_popen_error_uses_one_cpu_fallback_without_real_process(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            model_path = root / "demo-Q4_K_M.gguf"
            model_path.write_bytes(b"GGUF")
            cpu_executable = root / "llama-server.exe"
            cpu_executable.write_bytes(b"test-only-cpu")
            config_path = root / "local-backend.json"
            config = default_local_backend_config()
            config["llama_server_path"] = str(cpu_executable)
            save_local_backend_config(config, config_path)
            backend = ManagedLlamaCppBackend(config_path=config_path, executable=str(cpu_executable))
            model = GGUFModel(model_path, "demo", model_path.stat().st_size, quantization="Q4_K_M")
            commands: list[str] = []

            def fake_command(_model, runtime_id, prefer_experimental=True):
                commands.append(runtime_id)
                return ["vulkan-test"] if runtime_id == VULKAN_RUNTIME_ID else ["cpu-test"]

            with (
                mock.patch.object(backend, "preferred_standard_runtime", return_value=VULKAN_RUNTIME_ID),
                mock.patch.object(backend, "running", return_value=False),
                mock.patch.object(backend, "_prepare_port"),
                mock.patch.object(backend, "_command", side_effect=fake_command),
                mock.patch.object(backend, "executable_available", return_value=True),
                mock.patch.object(backend, "wait_ready", return_value=True),
                mock.patch.object(backend, "status", return_value={"status": "PASS"}),
                mock.patch("codex_ai_router.providers.local_backend.subprocess.Popen", side_effect=[OSError("vulkan start"), mock.Mock(pid=12347)]) as popen,
            ):
                self.assertEqual(backend.start(model), {"status": "PASS"})
            self.assertEqual(commands, [VULKAN_RUNTIME_ID, STANDARD_RUNTIME_ID])
            self.assertEqual(popen.call_count, 2)
            self.assertIn("VULKAN_START_OR_LOAD_FAILED", backend.state_path.read_text(encoding="utf-8"))

    def test_vulkan_popen_error_and_cpu_popen_error_stop_after_one_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            model_path = root / "demo-Q4_K_M.gguf"
            model_path.write_bytes(b"GGUF")
            cpu_executable = root / "llama-server.exe"
            cpu_executable.write_bytes(b"test-only-cpu")
            config_path = root / "local-backend.json"
            config = default_local_backend_config()
            config["llama_server_path"] = str(cpu_executable)
            save_local_backend_config(config, config_path)
            backend = ManagedLlamaCppBackend(config_path=config_path, executable=str(cpu_executable))
            model = GGUFModel(model_path, "demo", model_path.stat().st_size, quantization="Q4_K_M")
            with (
                mock.patch.object(backend, "preferred_standard_runtime", return_value=VULKAN_RUNTIME_ID),
                mock.patch.object(backend, "running", return_value=False),
                mock.patch.object(backend, "_prepare_port"),
                mock.patch.object(backend, "_command", side_effect=lambda _model, runtime_id, prefer_experimental=True: [runtime_id]),
                mock.patch.object(backend, "executable_available", return_value=True),
                mock.patch("codex_ai_router.providers.local_backend.subprocess.Popen", side_effect=[OSError("vulkan start"), OSError("cpu start")]) as popen,
            ):
                with self.assertRaisesRegex(ProviderError, "CPU_FALLBACK_START_FAILED"):
                    backend.start(model)
            self.assertEqual(popen.call_count, 2)

    def test_explicit_cpu_start_never_attempts_vulkan(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            model_path = root / "demo-Q4_K_M.gguf"
            model_path.write_bytes(b"GGUF")
            cpu_executable = root / "llama-server.exe"
            cpu_executable.write_bytes(b"test-only-cpu")
            config_path = root / "local-backend.json"
            config = default_local_backend_config()
            config["backend_preference"] = "CPU"
            config["llama_server_path"] = str(cpu_executable)
            save_local_backend_config(config, config_path)
            backend = ManagedLlamaCppBackend(config_path=config_path, executable=str(cpu_executable))
            model = GGUFModel(model_path, "demo", model_path.stat().st_size, quantization="Q4_K_M")
            commands: list[str] = []

            def fake_command(_model, runtime_id, prefer_experimental=True):
                commands.append(runtime_id)
                return ["cpu-test"]

            with (
                mock.patch.object(backend, "preferred_standard_runtime", return_value=STANDARD_RUNTIME_ID),
                mock.patch.object(backend, "running", return_value=False),
                mock.patch.object(backend, "_prepare_port"),
                mock.patch.object(backend, "_command", side_effect=fake_command),
                mock.patch.object(backend, "executable_available", return_value=True),
                mock.patch.object(backend, "wait_ready", return_value=True),
                mock.patch.object(backend, "status", return_value={"status": "PASS"}),
                mock.patch("codex_ai_router.providers.local_backend.subprocess.Popen", return_value=mock.Mock(pid=12348)) as popen,
            ):
                self.assertEqual(backend.start(model), {"status": "PASS"})
            self.assertEqual(commands, [STANDARD_RUNTIME_ID])
            self.assertEqual(popen.call_count, 1)


if __name__ == "__main__":
    unittest.main()

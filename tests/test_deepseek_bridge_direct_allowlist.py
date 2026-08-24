from __future__ import annotations

import unittest
from pathlib import Path
import tempfile
from unittest.mock import patch

from codex_ai_router.brain_providers import BrainProviderError
from codex_ai_router.deepseek_head_coordinator import DeepSeekHeadCoordinator
from codex_ai_router.local_agent import LocalAgent
from codex_ai_router.provider_allowlist import deepseek_bridge_direct_allowed, model_catalog, providers


class DeepSeekBridgeDirectAllowlistTests(unittest.TestCase):
    def test_direct_provider_is_internal_loopback_only_and_has_no_public_models(self):
        with patch("codex_ai_router.provider_allowlist._health", side_effect=[("OFFLINE", {})] * 6):
            record = next(item for item in providers() if item["id"] == "deepseek-bridge-direct")
            catalog = model_catalog()
        self.assertTrue(record["enabled"])
        self.assertEqual(record["endpoint"], "http://127.0.0.1:8791")
        self.assertEqual(record["scope"], "LOCAL_COORDINATOR_ONLY")
        self.assertFalse(record["api_only_exposed"])
        self.assertFalse(record["fallback_eligible"])
        self.assertEqual(record["models"], [])
        self.assertNotIn("deepseek-bridge-direct", {item["id"] for item in catalog})

    def test_only_approved_direct_endpoints_are_allowlisted(self):
        self.assertTrue(deepseek_bridge_direct_allowed("http://127.0.0.1:8791"))
        self.assertTrue(deepseek_bridge_direct_allowed("http://localhost:8791/"))
        self.assertFalse(deepseek_bridge_direct_allowed("http://192.168.137.1:8791"))
        self.assertFalse(deepseek_bridge_direct_allowed("https://127.0.0.1:8791"))

    def test_offline_direct_provider_is_not_allowlist_blocked(self):
        snapshot = lambda: [{
            "id": "deepseek-bridge-direct",
            "type": "DEEPSEEK_BRIDGE_DIRECT_INTERNAL",
            "enabled": True,
            "status": "OFFLINE",
            "scope": "LOCAL_COORDINATOR_ONLY",
            "fallback_eligible": False,
        }]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            coordinator = DeepSeekHeadCoordinator(root, LocalAgent(root, root / "agent"), provider_snapshot=snapshot)
            with patch("codex_ai_router.deepseek_head_coordinator.invoke_brain", side_effect=BrainProviderError("DEEPSEEK_BRIDGE_DIRECT_UNAVAILABLE")) as invoke:
                result = coordinator.coordinate({"task": "生成补丁草案，不修改文件", "brain_provider": "deepseek-bridge-direct", "invoke_brain": True, "collect_context": False, "allow_patch_draft": True})
        invoke.assert_called_once()
        self.assertEqual(result["error_code"], "DEEPSEEK_BRIDGE_DIRECT_UNAVAILABLE")
        self.assertNotEqual(result["error_code"], "DEEPSEEK_BRIDGE_DIRECT_ALLOWLIST_BLOCKED")
        self.assertEqual((result["patch_draft_source"], result["patch_draft_format_invalid"]), ("not_attempted", "NO"))


if __name__ == "__main__":
    unittest.main()

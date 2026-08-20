from __future__ import annotations

import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_ai_router import provider_config
from codex_ai_router.provider_setup import suggested_provider_type
from codex_ai_router.providers.groq import GroqProvider, classify_groq_error, groq_messages
from codex_ai_router.providers.base import DiscoveredModel
from codex_ai_router.providers.model_states import model_state_report
from codex_ai_router.providers.runtime_models import RuntimeModelState


class _Model:
    def __init__(self, model_id: str): self.id = model_id; self.owned_by = "groq"


class _Choice:
    def __init__(self): self.message = types.SimpleNamespace(content="GROQ_OK"); self.finish_reason = "stop"


class _Client:
    def __init__(self, api_key, timeout): self.models = types.SimpleNamespace(list=lambda: types.SimpleNamespace(data=[_Model("model-a")])) ; self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=self.create))
    @staticmethod
    def create(**kwargs): return types.SimpleNamespace(choices=[_Choice()], usage={})


class _Failure(Exception):
    def __init__(self, code): self.status_code = code


class GroqProviderTests(unittest.TestCase):
    def setUp(self): self.module = types.SimpleNamespace(Groq=_Client)

    def test_dependency_is_declared(self):
        root = Path(__file__).parents[1]
        self.assertIn('groq>=', (root / 'pyproject.toml').read_text(encoding='utf-8'))

    def test_standard_base_url_selects_groq_backend(self): self.assertEqual(suggested_provider_type('https://api.groq.com/openai/v1'), 'groq')

    def test_per_provider_key_env_is_used_without_exposure(self):
        with patch.dict(os.environ, {'TEST_GROQ_KEY': 'secret'}, clear=True), patch.dict(sys.modules, {'groq': self.module}):
            self.assertEqual(GroqProvider(key_env='TEST_GROQ_KEY').models(), ['model-a'])

    def test_sdk_discovery_and_chat_adapter(self):
        with patch.dict(os.environ, {'TEST_GROQ_KEY': 'secret'}, clear=True), patch.dict(sys.modules, {'groq': self.module}):
            provider = GroqProvider(key_env='TEST_GROQ_KEY')
            self.assertEqual((provider.discover_models()[0].model_id, provider.model_discovery_supported), ('model-a', 'YES'))
            provider.model = 'model-a'; self.assertEqual(provider.complete([{'role':'user','content':[{'text':'hi'}]}]).text, 'GROQ_OK')

    def test_messages_and_error_classification(self):
        self.assertEqual(groq_messages([{'role':'user','content':[{'text':'hi'}]}]), [{'role':'user','content':'hi'}])
        self.assertEqual(classify_groq_error(_Failure(403)), 'GROQ_PERMISSION_ERROR')

    def test_existing_provider_migration_preserves_key_reference_and_clears_headers(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'providers.json'
            provider_config.upsert('groq', {'type':'openai_compatible','base_url':'https://api.groq.com/openai/v1','api_key_env':'TEST_GROQ_KEY','headers':{'X-Unneeded':'ENV'}}, path)
            migrated = provider_config.migrate_to_groq('groq', path)
            self.assertEqual((migrated['type'], migrated['api_key_env'], migrated['headers']), ('groq','TEST_GROQ_KEY',{}))

    def test_probe_uses_small_bounded_request(self):
        with patch.dict(os.environ, {'TEST_GROQ_KEY': 'secret'}, clear=True), patch.dict(sys.modules, {'groq': self.module}):
            self.assertEqual(GroqProvider(key_env='TEST_GROQ_KEY').probe('model-a')['status'], 'PASS')

    def test_missing_key_is_a_classified_discovery_error(self):
        with patch.dict(os.environ, {}, clear=True):
            provider = GroqProvider(key_env='MISSING_GROQ_KEY')
            self.assertEqual((provider.models(), provider.remote_model_list_status), ([], 'GROQ_AUTH_ERROR'))

    def test_discovered_and_usable_models_are_persisted_for_control_panel(self):
        with tempfile.TemporaryDirectory() as temp:
            path, state_path = Path(temp) / 'providers.json', Path(temp) / 'runtime.json'
            provider_config.upsert('groq', {'type':'groq','base_url':'https://api.groq.com/openai/v1','api_key_env':'KEY'}, path)
            runtime = RuntimeModelState(state_path); runtime.record('groq', 'remote-model', 'PASS', 0.2)
            records = [DiscoveredModel('groq', 'remote-model', 'remote-model', None, {}, __import__('datetime').datetime.now())]
            states = model_state_report('groq', provider_config.load(path)['providers']['groq'], records, runtime=runtime)
            saved = provider_config.record_model_registry('groq', states, 'PASS', path)
            snapshot = saved['model_registry']
            self.assertEqual((snapshot['DISCOVERED_MODELS'], snapshot['USABLE_MODELS'], snapshot['CURRENT_RUNTIME_MODEL']), (['remote-model'], ['remote-model'], 'remote-model'))

    def test_runtime_selected_model_is_retained_when_refresh_has_no_remote_models(self):
        with tempfile.TemporaryDirectory() as temp:
            path, state_path = Path(temp) / 'providers.json', Path(temp) / 'runtime.json'
            provider_config.upsert('groq', {'type':'groq','base_url':'https://api.groq.com/openai/v1','api_key_env':'KEY'}, path)
            runtime = RuntimeModelState(state_path); runtime.record('groq', 'runtime-only', 'PASS', 0.1)
            states = model_state_report('groq', provider_config.load(path)['providers']['groq'], [], runtime=runtime)
            saved = provider_config.record_model_registry('groq', states, 'GROQ_AUTH_ERROR', path)
            self.assertEqual((saved['model_registry']['DISCOVERED_MODELS'], saved['model_registry']['SOURCES']['runtime-only']), (['runtime-only'], 'RUNTIME_PROBE'))

    def test_failed_refresh_does_not_clear_previous_discovery(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'providers.json'
            provider_config.upsert('groq', {'type':'groq','base_url':'https://api.groq.com/openai/v1','api_key_env':'KEY'}, path)
            provider_config.record_discovery('groq', ['old-model'], 'PASS', path)
            self.assertEqual(provider_config.record_discovery('groq', [], 'GROQ_AUTH_ERROR', path)['last_discovery_models'], ['old-model'])


if __name__ == '__main__': unittest.main()

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
from codex_ai_router.providers.base import ProviderResponse
from codex_ai_router.network import NetworkMode


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

    def test_manual_runtime_model_selection_is_persisted_and_rejects_unknown_models(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'providers.json'
            provider_config.upsert('groq', {'type':'groq','base_url':'https://api.groq.com/openai/v1','model_registry':{'USABLE_MODELS':['a'], 'DENIED_MODELS':[]}}, path)
            self.assertEqual(provider_config.set_runtime_model_preference('groq', 'a', path)['preferred_runtime_model'], 'a')
            with self.assertRaises(ValueError): provider_config.set_runtime_model_preference('groq', 'b', path)

    def test_server_lists_and_routes_provider_specific_groq_virtual_model(self):
        from codex_ai_router import server
        class FakeGroq:
            def __init__(self, **kwargs): self.provider_id = kwargs['provider_id']; self.model = ''
            def complete(self, prompt, max_tokens=16): return ProviderResponse('GROQ_ROUTER_OK', self.model, usage={})
        metadata = {'providers': {'groq-2': {'type':'groq','enabled':True,'api_key_env':'KEY','model_registry':{'ALLOWED_MODELS':['allam-2-7b'], 'USABLE_MODELS':['allam-2-7b']}}}}
        with tempfile.TemporaryDirectory() as temp, patch('codex_ai_router.server.provider_config.load', return_value=metadata), patch('codex_ai_router.server.GroqProvider', FakeGroq):
            state = RuntimeModelState(Path(temp) / 'runtime.json'); state.record('groq-2', 'allam-2-7b', 'PASS', 0.1)
            service = server.RouterService(NetworkMode.AUTO, runtime_models=state)
            self.assertIn('xiaoyu-api-groq-2', [model['id'] for model in service.models()])
            response = service.respond({'model':'xiaoyu-api-groq-2','input':'only reply','max_output_tokens':16})
            self.assertEqual((response['object'], response['output_text']), ('response', 'GROQ_ROUTER_OK'))

    def test_auto_prefers_runtime_groq_provider_and_manual_choice_is_not_overridden(self):
        from codex_ai_router import server
        metadata = {'providers': {'groq-2': {'type':'groq','enabled':True,'model_registry':{'ALLOWED_MODELS':['a','b'], 'USABLE_MODELS':['a','b']}, 'preferred_runtime_model':'b'}}}
        with tempfile.TemporaryDirectory() as temp, patch('codex_ai_router.server.provider_config.load', return_value=metadata):
            state = RuntimeModelState(Path(temp) / 'runtime.json'); state.record('groq-2', 'a', 'PASS', 0.01); state.record('groq-2', 'b', 'PASS', 0.2)
            service = server.RouterService(NetworkMode.AUTO, runtime_models=state)
            self.assertEqual(service._select_text_model_for_entry('groq-2', metadata['providers']['groq-2']), 'b')

    def test_denied_model_cannot_be_selected_and_cooldown_can_be_cleared(self):
        with tempfile.TemporaryDirectory() as temp:
            path, state_path = Path(temp) / 'providers.json', Path(temp) / 'runtime.json'
            provider_config.upsert('groq', {'type':'groq','base_url':'https://api.groq.com/openai/v1','model_registry':{'DISCOVERED_MODELS':['a'], 'USABLE_MODELS':['a'], 'DENIED_MODELS':[]}}, path)
            provider_config.set_model_denied('groq', 'a', True, path)
            with self.assertRaises(ValueError): provider_config.set_runtime_model_preference('groq', 'a', path)
            state = RuntimeModelState(state_path); state.record('groq', 'a', 'TIMEOUT', 20); state.clear_cooldown('groq', 'a')
            self.assertFalse(state.recent_timeout('groq', 'a'))

    def test_batch_policy_deny_clears_selected_and_refresh_preserves_policy(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'providers.json'
            provider_config.upsert('groq', {'type':'groq','base_url':'https://api.groq.com/openai/v1','model_registry':{'DISCOVERED_MODELS':['a','b'], 'USABLE_MODELS':['a','b'], 'CURRENT_RUNTIME_MODEL':'a', 'RUNTIME_RESPONSIVE_MODELS':['a','b']}}, path)
            changed = provider_config.batch_model_policy('groq', ['a'], 'deny', path=path)
            self.assertEqual((changed['model_registry']['CURRENT_RUNTIME_MODEL'], changed['model_registry']['USABLE_MODELS']), (None, ['b']))
            refreshed = provider_config.record_model_registry('groq', {'DISCOVERED_MODELS':['a','b'], 'RUNTIME_RESPONSIVE_MODELS':['a','b'], 'TIMEOUT_COOLDOWN_MODELS':[], 'CURRENT_RUNTIME_MODEL':'a'}, 'PASS', path)
            self.assertEqual((refreshed['denied_model_ids'], refreshed['model_registry']['CURRENT_RUNTIME_MODEL']), (['a'], None))

    def test_priority_and_provider_priority_affect_auto_selection(self):
        from codex_ai_router import server
        metadata = {'providers': {
            'slow': {'type':'groq','enabled':True,'priority':10,'model_registry':{'ALLOWED_MODELS':['a'], 'USABLE_MODELS':['a']}, 'model_priorities':{'a':100}},
            'fast': {'type':'groq','enabled':True,'priority':20,'model_registry':{'ALLOWED_MODELS':['b'], 'USABLE_MODELS':['b']}, 'model_priorities':{'b':1}},
        }}
        with tempfile.TemporaryDirectory() as temp, patch('codex_ai_router.server.provider_config.load', return_value=metadata):
            state=RuntimeModelState(Path(temp)/'runtime.json');state.record('slow','a','PASS',1);state.record('fast','b','PASS',0.01)
            service=server.RouterService(NetworkMode.AUTO,runtime_models=state)
            self.assertEqual(service._api_provider('xiaoyu-api-auto')[0], 'slow')


if __name__ == '__main__': unittest.main()

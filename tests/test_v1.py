import os
import tempfile
import time
import unittest
import subprocess
import json
import io
import sys
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from codex_ai_router.classifier import classify
from codex_ai_router.policy import choose_mode
from codex_ai_router.providers.lmstudio import LMStudioProvider
from codex_ai_router.providers.openai_compatible import OpenAICompatibleProvider
from codex_ai_router.result import AgentResult
from codex_ai_router.router import Router
from codex_ai_router.security.permissions import canonical_inside
from codex_ai_router.security.secrets import redact
from codex_ai_router.task import Mode, Risk
from codex_ai_router.tools.subprocess_runner import run_command
from codex_ai_router.tools.worktree import isolated_worktree
from codex_ai_router.configuration import load_config
from codex_ai_router.model_policy import SelectionPolicy
from codex_ai_router.providers.base import ProviderError
from codex_ai_router.providers.openai_compatible import OpenAICompatibleProvider, ResponsesResponseAdapter
from codex_ai_router.providers.registry import ModelRegistry
from codex_ai_router.providers.base import DiscoveredModel
from codex_ai_router.provider_setup import classify_probe, default_display_name, suggested_provider_id, suggested_provider_type
from codex_ai_router import provider_config
from urllib.error import HTTPError
from datetime import datetime, timezone


class FakeProvider:
    def __init__(self, available=True, output=None): self._available, self.output, self.calls = available, output or '{"status":"PASS","summary":"ok","confidence":0.9,"risk":"LOW","needs_escalation":false,"actions":[],"tests":[],"warnings":[]}', 0
    def available(self): return self._available
    def models(self): return ["fake"] if self._available else []
    def ask(self, prompt): self.calls += 1; return self.output


class FakeHeaders:
    def __init__(self, content_type='application/json'): self.content_type = content_type
    def get_content_type(self): return self.content_type


class FakeResponse:
    def __init__(self, data, content_type='application/json'): self.data, self.headers = data, FakeHeaders(content_type)
    def read(self): return self.data
    def __enter__(self): return self
    def __exit__(self, *args): return False


class RouterV1Tests(unittest.TestCase):
    def router(self, local=True, api=True): return Router(Path.cwd(), FakeProvider(local), FakeProvider(api))
    def test_01_local_model_discovery(self):
        with patch('codex_ai_router.providers.lmstudio.urlopen') as open_: open_.return_value.__enter__.return_value.read.return_value=b'{"data":[{"id":"model"}]}' ; self.assertEqual(LMStudioProvider().models(), ["model"])
    def test_02_api_missing_key(self):
        with patch.dict(os.environ, {}, clear=True): self.assertFalse(OpenAICompatibleProvider().available())
    def test_03_api_key_not_repr(self): self.assertNotIn('secret', repr(OpenAICompatibleProvider()))
    def test_03b_api_key_env_is_selectable(self):
        with patch.dict(os.environ, {'XIAOYU_CODER_API_KEY_ENV': 'XIAOYU_API_PROVIDER_KEY'}, clear=True):
            self.assertEqual(OpenAICompatibleProvider().key_env, 'XIAOYU_API_PROVIDER_KEY')
    def test_04_local_only(self): self.assertEqual(self.router().route('summarize README', Mode.LOCAL_ONLY)['mode'], 'LOCAL_ONLY')
    def test_05_api_only(self): self.assertEqual(self.router().delegate('generate tests', Mode.API_ONLY).status, 'PASS')
    def test_06_api_local_mode(self): self.assertEqual(self.router().route('fix function', Mode.API_LOCAL)['mode'], 'API_LOCAL')
    def test_07_auto_low(self): self.assertEqual(self.router().route('summarize README')['mode'], 'LOCAL_ONLY')
    def test_08_high_escalates(self): self.assertEqual(self.router().delegate('production deploy now').status, 'CODEX_ACTION_REQUIRED')
    def test_09_redaction(self): self.assertNotIn('abc123', redact('Bearer abc123'))
    def test_10_path_traversal(self):
        with tempfile.TemporaryDirectory() as temp: self.assertRaises(PermissionError, canonical_inside, Path(temp), Path(temp).parent / 'outside')
    def test_11_worktree_isolation(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            subprocess.run(['git', 'init'], cwd=repo, check=True, capture_output=True)
            subprocess.run(['git', 'config', 'user.email', 'router@example.invalid'], cwd=repo, check=True)
            subprocess.run(['git', 'config', 'user.name', 'Router Test'], cwd=repo, check=True)
            (repo / 'file.txt').write_text('original', encoding='utf-8')
            subprocess.run(['git', 'add', '.'], cwd=repo, check=True); subprocess.run(['git', 'commit', '-m', 'initial'], cwd=repo, check=True, capture_output=True)
            with isolated_worktree(repo, 'local') as isolated:
                (isolated / 'file.txt').write_text('changed', encoding='utf-8')
                self.assertEqual((repo / 'file.txt').read_text(encoding='utf-8'), 'original')
        self.assertEqual(self.router().status()['MULTI_AGENT_SHARED_WRITE_TREE'], 'NO')
    def test_12_command_timeout(self):
        code, output = run_command(['python', '-c', 'import time; time.sleep(1)'], Path.cwd(), timeout=0.01); self.assertEqual(code, 124); self.assertIn('TIMEOUT', output)
    def test_13_output_limit(self): self.assertLessEqual(len(run_command(['python','-c','print("x"*1000)'], Path.cwd(), output_limit=10)[1]), 10)
    def test_14_max_iterations_stop(self): self.assertTrue(__import__('codex_ai_router.agents.coder', fromlist=['agent_loop']).agent_loop(FakeProvider(), 'x', Path.cwd(), 'LOW', 0).needs_escalation)
    def test_15_local_fallback(self): self.assertEqual(self.router(False, True).delegate('summarize README').status, 'PASS')
    def test_16_api_fallback(self): self.assertEqual(self.router(True, False).delegate('generate tests', Mode.API_ONLY).status, 'PASS')
    def test_17_structured_retry(self):
        from codex_ai_router.agents.coder import ask_structured
        self.assertEqual(ask_structured(FakeProvider(output='bad'), 'x', 'LOW').status, 'TEXT_ONLY_RESULT')
    def test_17b_provider_error_escalates(self):
        from codex_ai_router.agents.coder import ask_structured
        from codex_ai_router.providers.base import ProviderError
        class ErrorProvider(FakeProvider):
            def ask(self, prompt): raise ProviderError('unavailable')
        self.assertTrue(ask_structured(ErrorProvider(), 'x', 'LOW').needs_escalation)
    def test_18_disagreement(self):
        from codex_ai_router.orchestration.judge import judge
        self.assertTrue(judge(AgentResult('PASS','',risk='LOW'), AgentResult('PASS','',risk='HIGH')).needs_escalation)
    def test_19_codex_only(self): self.assertEqual(self.router().delegate('x', Mode.CODEX_ONLY).status, 'CODEX_ACTION_REQUIRED')
    def test_20_config_parsing(self):
        config = load_config(Path(__file__).parents[1] / 'config' / 'config.example.yaml')
        self.assertEqual(config['api']['api_key_env'], 'XIAOYU_CODER_API_KEY')

    def test_21_allowlisted_model_can_run(self):
        policy = SelectionPolicy.from_values(allow_model=['api:fake'])
        self.assertEqual(Router(Path.cwd(), FakeProvider(), FakeProvider(), selection_policy=policy).delegate('tests', Mode.API_ONLY).status, 'PASS')
    def test_22_denied_model_never_runs(self):
        policy = SelectionPolicy.from_values(deny_model=['api:fake'])
        self.assertEqual(Router(Path.cwd(), FakeProvider(), FakeProvider(), selection_policy=policy).delegate('tests', Mode.API_ONLY).status, 'NO_ELIGIBLE_MODEL')
    def test_23_denied_provider_never_runs(self):
        policy = SelectionPolicy.from_values(deny_provider=['api'])
        self.assertEqual(Router(Path.cwd(), FakeProvider(), FakeProvider(), selection_policy=policy).delegate('tests', Mode.API_ONLY).status, 'NO_ELIGIBLE_MODEL')
    def test_24_override_cannot_bypass_deny(self):
        policy = SelectionPolicy.from_values(deny_model=['api:fake'])
        self.assertEqual(Router(Path.cwd(), FakeProvider(), FakeProvider(), selection_policy=policy).delegate('tests', Mode.API_ONLY, api_model='fake').status, 'NO_ELIGIBLE_MODEL')
    def test_24b_denied_override_cannot_fall_back(self):
        policy = SelectionPolicy.from_values(deny_model=['local:blocked'])
        self.assertIsNone(policy.choose('local', ['blocked', 'allowed'], 'coder', 'blocked'))
    def test_25_role_preference_respects_deny(self):
        policy = SelectionPolicy.from_values(deny_model=['api:fake'], roles={'coder': ('api:fake',)})
        self.assertIsNone(policy.choose('api', ['fake'], 'coder'))
    def test_26_fallback_respects_deny(self):
        policy = SelectionPolicy.from_values(deny_provider=['local'], deny_model=['api:fake'])
        self.assertEqual(Router(Path.cwd(), FakeProvider(), FakeProvider(), selection_policy=policy).delegate('summarize README').status, 'CODEX_ACTION_REQUIRED')
    def test_27_parallel_review_respects_deny(self):
        from codex_ai_router.orchestration.parallel import parallel_review
        policy = SelectionPolicy.from_values(deny_provider=['api'])
        local, api = FakeProvider(), FakeProvider()
        self.assertTrue(parallel_review(local, api, 'diff', 'LOW', policy).needs_escalation)
        self.assertEqual((local.calls, api.calls), (0, 0))
    def test_28_context_compactor_respects_deny(self):
        from codex_ai_router.agents.analyst import analyze
        policy = SelectionPolicy.from_values(deny_model=['local:fake'], roles={'context_compactor': ('local:fake',)})
        self.assertIsNone(policy.choose('local', ['fake'], 'context_compactor'))
    def test_29_no_api_blocks_external(self):
        policy = SelectionPolicy.from_values(no_api=True)
        self.assertFalse(policy.providers.permits('api'))
    def test_30_no_eligible_model_stops(self):
        policy = SelectionPolicy.from_values(allow_model=['api:other'])
        self.assertEqual(Router(Path.cwd(), FakeProvider(), FakeProvider(), selection_policy=policy).delegate('tests', Mode.API_ONLY).status, 'NO_ELIGIBLE_MODEL')
    def test_31_chat_completions_backward_compatible(self):
        data = b'{"model":"m","choices":[{"message":{"content":"ok"},"finish_reason":"stop"}]}'
        with patch('codex_ai_router.providers.openai_compatible.urlopen', return_value=FakeResponse(data)):
            self.assertEqual(OpenAICompatibleProvider('https://host/v1', 'm', wire_api='chat_completions', requires_bearer_auth=False).complete('x').text, 'ok')
    def test_32_responses_request_shape(self):
        data = b'{"model":"m","status":"completed","output_text":"ok"}'
        with patch('codex_ai_router.providers.openai_compatible.urlopen', return_value=FakeResponse(data)) as open_:
            self.assertEqual(OpenAICompatibleProvider('https://host', 'm', wire_api='responses', requires_bearer_auth=False).complete('x').text, 'ok')
            request = open_.call_args.args[0]
            self.assertEqual(request.full_url, 'https://host/v1/responses')
            self.assertEqual(json.loads(request.data), {'model': 'm', 'input': 'x'})
    def test_33_responses_text_extraction(self): self.assertEqual(ResponsesResponseAdapter.text({'output':[{'content':[{'text':'a'}, {'output_text':'b'}]}]}), 'ab')
    def test_34_responses_base_url_without_v1(self): self.assertEqual(OpenAICompatibleProvider('https://host', 'm', wire_api='responses').endpoint(), 'https://host/v1/responses')
    def test_35_responses_base_url_with_v1(self): self.assertEqual(OpenAICompatibleProvider('https://host/v1', 'm', wire_api='responses').endpoint(), 'https://host/v1/responses')
    def test_36_no_bearer_when_auth_disabled(self): self.assertNotIn('Authorization', OpenAICompatibleProvider('https://host', 'm', requires_bearer_auth=False).request_headers())
    def test_37_bearer_when_auth_enabled(self):
        with patch.dict(os.environ, {'XIAOYU_CODER_API_KEY': 'test-key'}, clear=True): self.assertIn('Authorization', OpenAICompatibleProvider('https://host', 'm').request_headers())
    def test_38_custom_header_env(self):
        with patch.dict(os.environ, {'TEST_HEADER_ENV': 'selector'}, clear=True): self.assertEqual(OpenAICompatibleProvider('https://host', 'm', requires_bearer_auth=False, header_env={'X-Selector':'TEST_HEADER_ENV'}).request_headers()['X-Selector'], 'selector')
    def test_39_html_200_rejected(self):
        with patch('codex_ai_router.providers.openai_compatible.urlopen', return_value=FakeResponse(b'<html>', 'text/html')):
            with self.assertRaisesRegex(ProviderError, 'NON_API_RESPONSE'): OpenAICompatibleProvider('https://host', 'm', requires_bearer_auth=False).complete('x')
    def test_40_deny_model_no_provider_call(self):
        provider = FakeProvider()
        policy = SelectionPolicy.from_values(deny_model=['api:fake'])
        self.assertEqual(Router(Path.cwd(), FakeProvider(), provider, selection_policy=policy).delegate('tests', Mode.API_ONLY).status, 'NO_ELIGIBLE_MODEL')
        self.assertEqual(provider.calls, 0)
    def test_41_model_discovery_from_v1_models(self):
        with patch('codex_ai_router.providers.openai_compatible.urlopen', return_value=FakeResponse(b'{"data":[{"id":"m","owned_by":"o"}]}')) as open_:
            models = OpenAICompatibleProvider('https://host', requires_bearer_auth=False).discover_models()
            self.assertEqual(models[0].qualified_id, 'api:m'); self.assertEqual(open_.call_args.args[0].full_url, 'https://host/v1/models')
    def test_42_discovery_does_not_require_manual_name(self):
        with patch('codex_ai_router.providers.openai_compatible.urlopen', return_value=FakeResponse(b'{"data":[{"id":"m"}]}')):
            self.assertEqual(OpenAICompatibleProvider('https://host', requires_bearer_auth=False).candidate_models(), ['m'])
    def test_43_lmstudio_models_auto_registered(self):
        with patch.object(LMStudioProvider, 'models', return_value=['one', 'two']): self.assertEqual([x.model_id for x in LMStudioProvider().discover_models()], ['one', 'two'])
    def test_44_responses_provider_can_discover_models(self):
        with patch('codex_ai_router.providers.openai_compatible.urlopen', return_value=FakeResponse(b'{"data":[{"id":"m"}]}')):
            self.assertEqual(OpenAICompatibleProvider('https://host', wire_api='responses', requires_bearer_auth=False).models(), ['m'])
    def test_45_discovered_not_automatically_allowed(self): self.assertEqual(SelectionPolicy.from_values(allow_model=['api:other']).eligible('api', ['m']), [])
    def test_46_deny_filters_discovered_model(self): self.assertEqual(SelectionPolicy.from_values(deny_model=['api:m']).eligible('api', ['m']), [])
    def test_47_eligible_models_correct(self): self.assertEqual(SelectionPolicy.from_values(allow_model=['api:m']).eligible('api', ['m', 'other']), ['m'])
    def test_48_model_cache_refresh(self):
        with patch('codex_ai_router.providers.openai_compatible.urlopen', return_value=FakeResponse(b'{"data":[{"id":"m"}]}')) as open_:
            provider = OpenAICompatibleProvider('https://cache-host', requires_bearer_auth=False); provider.models(); provider.models(); self.assertEqual(open_.call_count, 1); provider.refresh_models(); self.assertEqual(open_.call_count, 2)
    def test_49_unsupported_discovery_allows_manual_fallback(self):
        error = HTTPError('https://host/v1/models', 404, 'missing', None, None)
        with patch('codex_ai_router.providers.openai_compatible.urlopen', side_effect=error):
            provider = OpenAICompatibleProvider('https://unsupported', 'manual', requires_bearer_auth=False); self.assertEqual(provider.candidate_models(), [])
    def test_50_html_model_response_rejected(self):
        with patch('codex_ai_router.providers.openai_compatible.urlopen', return_value=FakeResponse(b'<html>', 'text/html')):
            provider = OpenAICompatibleProvider('https://html-models', requires_bearer_auth=False); self.assertEqual(provider.models(), []); self.assertEqual(provider.model_discovery_supported, 'NON_API_RESPONSE')
    def test_51_removed_model_marked_unavailable(self):
        registry = ModelRegistry(); model = DiscoveredModel('api', 'm', 'm', None, None, datetime.now(timezone.utc)); registry.update('api', [model]); registry.update('api', []); self.assertEqual(registry.all()[0].availability, 'NO')
    def test_52_model_not_found_triggers_single_refresh(self):
        response = FakeResponse(b'{"data":[{"id":"other"}]}')
        error = HTTPError('https://host/v1/responses', 404, 'missing', None, None)
        with patch('codex_ai_router.providers.openai_compatible.urlopen', side_effect=[error, response]) as open_:
            with self.assertRaisesRegex(ProviderError, 'MODEL_NOT_FOUND'): OpenAICompatibleProvider('https://not-found', 'missing', wire_api='responses', requires_bearer_auth=False).complete('x')
            self.assertEqual(open_.call_count, 2)
    def test_53_hostname_generates_provider_id(self): self.assertEqual(suggested_provider_id('https://api.example.com/v1'), 'api-example')
    def test_54_known_provider_auto_named(self): self.assertEqual(suggested_provider_id('https://lightboat.dpdns.org'), 'lightboat')
    def test_55_unknown_hostname_safe_id(self): self.assertEqual(suggested_provider_id('https://api.example.com', {'api-example'}), 'api-example-2')
    def test_56_manual_id_override_is_supported(self): self.assertEqual('my-lightboat', 'my-lightboat')
    def test_57_provider_type_auto_detect(self): self.assertEqual(suggested_provider_type('http://127.0.0.1:1234/v1'), 'lmstudio')
    def test_58_wire_api_probe(self): self.assertEqual(classify_probe('application/json', 401, None), 'responses')
    def test_59_html_probe_rejected(self): self.assertIsNone(classify_probe('text/html', 200, 200))
    def test_60_generated_id_is_lowercase(self): self.assertEqual(suggested_provider_id('https://API.Example.COM'), 'api-example')
    def test_61_generated_id_matches_validation(self):
        provider_id = suggested_provider_id('https://strange_host.example.com')
        provider_config.validate_provider_id(provider_id)
    def test_62_lightboat_hostname_generates_lightboat(self): self.assertEqual(suggested_provider_id('https://lightboat.dpdns.org'), 'lightboat')
    def test_63_unicode_display_name_allowed(self):
        with tempfile.TemporaryDirectory() as temp:
            record = provider_config.upsert('lightboat', {'display_name': '轻舟公益站', 'type': 'openai_compatible'}, Path(temp) / 'providers.json')
            self.assertEqual(record['display_name'], '轻舟公益站')
    def test_64_chinese_display_name_roundtrip(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'providers.json'
            provider_config.upsert('lightboat', {'display_name': '轻舟公益站', 'type': 'openai_compatible'}, path)
            self.assertEqual(provider_config.load(path)['providers']['lightboat']['display_name'], '轻舟公益站')
            self.assertIn('轻舟公益站', path.read_text(encoding='utf-8'))
    def test_65_display_name_does_not_change_provider_id(self):
        with tempfile.TemporaryDirectory() as temp:
            record = provider_config.upsert('lightboat', {'display_name': 'DeepSeek 主接口', 'type': 'openai_compatible'}, Path(temp) / 'providers.json')
            self.assertEqual(record['id'], 'lightboat')
    def test_66_duplicate_display_name_allowed(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'providers.json'
            provider_config.upsert('one', {'display_name': '同名', 'type': 'openai_compatible'}, path)
            provider_config.upsert('two', {'display_name': '同名', 'type': 'openai_compatible'}, path)
            self.assertEqual(len(provider_config.load(path)['providers']), 2)
    def test_67_duplicate_provider_id_gets_suffix(self): self.assertEqual(suggested_provider_id('https://lightboat.dpdns.org', {'lightboat'}), 'lightboat-2')
    def test_68_legacy_provider_without_display_name(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'providers.json'
            path.write_text('{"providers":{"legacy":{"type":"openai_compatible"}}}', encoding='utf-8')
            self.assertEqual(provider_config.load(path)['providers']['legacy']['display_name'], 'legacy')
    def test_69_model_id_uses_provider_id_not_display_name(self):
        model = DiscoveredModel('lightboat', 'gpt-x', '轻舟公益站', None, None, datetime.now(timezone.utc))
        self.assertEqual(model.qualified_id, 'lightboat:gpt-x')
    def test_70_default_display_name_is_separate_from_id(self): self.assertEqual(default_display_name('nvidia-backup'), 'Nvidia Backup')
    def test_71_provider_list_exposes_display_name_and_id(self):
        from codex_ai_router import cli
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'providers.json'
            provider_config.upsert('lightboat', {'display_name': '轻舟公益站', 'type': 'openai_compatible'}, path)
            output = io.StringIO()
            with patch.dict(os.environ, {'XIAOYU_ROUTER_PROVIDER_CONFIG': str(path)}, clear=False), patch.object(sys, 'argv', ['xiaoyu-router', 'provider', 'list']), redirect_stdout(output):
                cli.main()
            listed = json.loads(output.getvalue())['providers'][0]
            self.assertEqual((listed['display_name'], listed['id']), ('轻舟公益站', 'lightboat'))
    def test_72_legacy_metadata_migrates_to_canonical_store(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); legacy, canonical = root / 'provider-header-mappings.json', root / 'providers.json'
            legacy.write_text(json.dumps({'providers': {'test-provider': {'type': 'openai_compatible', 'base_url': 'https://example.invalid/v1', 'api_key_env': 'TEST_PROVIDER_KEY', 'headers': {'X-Selector': 'TEST_SELECTOR_ENV'}}}}), encoding='utf-8')
            data, migrated, orphaned = provider_config.migrate_legacy_metadata(canonical, legacy)
            self.assertEqual((migrated, orphaned), (['test-provider'], []))
            self.assertEqual(data['providers']['test-provider']['headers']['X-Selector'], 'TEST_SELECTOR_ENV')
    def test_73_invalid_legacy_metadata_is_not_migrated(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); legacy, canonical = root / 'provider-header-mappings.json', root / 'providers.json'
            legacy.write_text(json.dumps({'providers': {'领取': {'headers': {'X-Selector': 'TEST_SELECTOR_ENV'}}}}), encoding='utf-8')
            data, migrated, orphaned = provider_config.migrate_legacy_metadata(canonical, legacy)
            self.assertEqual((data['providers'], migrated, orphaned), ({}, [], ['领取']))
    def test_74_explicit_provider_config_override_does_not_import_user_legacy(self):
        with tempfile.TemporaryDirectory() as temp:
            override = Path(temp) / 'fixture-providers.json'
            with patch.dict(os.environ, {'XIAOYU_ROUTER_PROVIDER_CONFIG': str(override)}, clear=False):
                self.assertEqual(provider_config.load(), {'providers': {}})
    def test_75_default_provider_path_uses_windows_userprofile(self):
        with patch.dict(os.environ, {'USERPROFILE': 'C:/router-user'}, clear=False):
            self.assertEqual(provider_config.config_path(), Path('C:/router-user/.codex-ai-router/providers.json'))
    def test_76_model_list_401_falls_back(self):
        error = HTTPError('https://host/v1/models', 401, 'unauthorized', None, None)
        response = FakeResponse(b'{"model":"configured","output_text":"API_ROUTER_OK"}')
        with patch.dict(os.environ, {}, clear=True), patch('codex_ai_router.providers.openai_compatible.urlopen', side_effect=[error, response]):
            provider = OpenAICompatibleProvider('https://host', wire_api='responses', requires_bearer_auth=False, provider_id='api1', provider_metadata={'models':['configured']})
            self.assertEqual(provider.models(), ['configured']); self.assertEqual(provider.remote_model_list_status, 'HTTP_401')
    def test_77_configured_model_candidate_detected(self):
        from codex_ai_router.providers.model_discovery import ModelDiscoveryChain
        self.assertIn(('configured','EXISTING_PROVIDER_METADATA'), ModelDiscoveryChain('api','https://host',{'models':['configured']}).candidates())
    def test_78_codex_profile_model_metadata_detected(self):
        from codex_ai_router.providers.model_discovery import codex_profile_candidates
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'config.toml'; path.write_text('model = "wrong"\n[model_providers.match]\nbase_url = "https://host/v1"\nmodel = "profile-model"\n', encoding='utf-8')
            self.assertIn('profile-model', codex_profile_candidates('https://host', path))
    def test_78b_codex_profile_auth_metadata_detected(self):
        from codex_ai_router.providers.model_discovery import codex_profile_requires_bearer_auth
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'config.toml'; path.write_text('[model_providers.match]\nbase_url = "https://host/v1"\nrequires_openai_auth = false\n', encoding='utf-8')
            self.assertFalse(codex_profile_requires_bearer_auth('https://host', path))
    def test_79_invalid_candidate_not_registered(self):
        error = HTTPError('https://host/v1/models', 401, 'unauthorized', None, None)
        response = FakeResponse(b'{"model":"bad","output_text":"not the expected value"}')
        with patch.dict(os.environ, {}, clear=True), patch('codex_ai_router.providers.openai_compatible.urlopen', side_effect=[error, response]):
            provider = OpenAICompatibleProvider('https://host', wire_api='responses', requires_bearer_auth=False, provider_metadata={'models':['bad']})
            self.assertEqual(provider.models(), [])
    def test_80_validated_candidate_registered(self):
        error = HTTPError('https://host/v1/models', 403, 'forbidden', None, None)
        response = FakeResponse(b'{"model":"good","output_text":"API_ROUTER_OK"}')
        with patch.dict(os.environ, {}, clear=True), patch('codex_ai_router.providers.openai_compatible.urlopen', side_effect=[error, response]):
            provider = OpenAICompatibleProvider('https://host', wire_api='responses', requires_bearer_auth=False, provider_id='api1', provider_metadata={'models':['good']})
            model = provider.discover_models()[0]; self.assertEqual((model.qualified_id, model.raw_metadata['validation']), ('api1:good','PASS'))
    def test_81_strict_and_fenced_json_parse(self):
        from codex_ai_router.agents.structured import parse_structured
        self.assertEqual(parse_structured('{"status":"PASS"}')['status'], 'PASS'); self.assertEqual(parse_structured('```json\n{"status":"PASS"}\n```')['status'], 'PASS')
    def test_82_json_with_explanation_and_repair(self):
        from codex_ai_router.agents.structured import parse_structured
        self.assertEqual(parse_structured('Explanation. {"status":"PASS",} done')['status'], 'PASS')
    def test_83_deterministic_repair_does_not_invent_action(self):
        from codex_ai_router.agents.structured import parse_structured
        self.assertNotIn('actions', parse_structured('{"status":"PASS",}'))
    def test_84_single_repair_retry(self):
        from codex_ai_router.agents.coder import ask_structured
        class SequenceProvider(FakeProvider):
            def __init__(self): super().__init__(); self.values = ['bad', '{"status":"PASS","summary":"ok","confidence":1,"risk":"LOW","needs_escalation":false,"actions":[],"tests":[],"warnings":[]}']
            def ask(self, prompt): self.calls += 1; return self.values.pop(0)
        provider = SequenceProvider(); self.assertEqual(ask_structured(provider, 'summarize', 'LOW').status, 'PASS'); self.assertEqual(provider.calls, 2)
    def test_85_readonly_text_fallback(self):
        from codex_ai_router.agents.coder import ask_structured
        self.assertEqual(ask_structured(FakeProvider(output='plain summary'), 'summarize README', 'LOW').status, 'TEXT_ONLY_RESULT')
    def test_86_coding_action_requires_structure(self):
        from codex_ai_router.agents.coder import ask_structured
        self.assertEqual(ask_structured(FakeProvider(output='edit this file'), 'edit file and run tests', 'LOW').status, 'STRUCTURED_ACTION_UNAVAILABLE')
    def test_87_local_model_structured_fallback(self):
        class LocalModels(FakeProvider):
            def __init__(self): super().__init__(); self.model = 'one'; self.attempts = 0
            def models(self): return ['one','two']
            def ask(self, prompt):
                self.attempts += 1
                return 'bad' if self.model == 'one' else '{"status":"PASS","summary":"ok","confidence":1,"risk":"LOW","needs_escalation":false,"actions":[],"tests":[],"warnings":[]}'
        local = LocalModels(); router = Router(Path.cwd(), local, FakeProvider()); result = router.delegate('edit file and run tests', Mode.LOCAL_ONLY)
        self.assertEqual((result.status, local.model), ('PASS','two'))
    def test_88_runtime_capability_records_structure_without_granting_tools(self):
        local = FakeProvider(output='{"status":"PASS","summary":"ok","confidence":1,"risk":"LOW","needs_escalation":false,"actions":[],"tests":[],"warnings":[]}')
        local.model = 'local-model'
        local.models = lambda: ['local-model']
        router = Router(Path.cwd(), local, FakeProvider())
        router.delegate('summarize README', Mode.LOCAL_ONLY)
        capabilities = router.model_registry.capabilities('local:local-model')
        self.assertIn('TEXT', capabilities); self.assertIn('STRUCTURED_OUTPUT', capabilities); self.assertNotIn('TOOL_CALLING', capabilities)
    def test_89_local_requests_have_bounded_non_streaming_generation(self):
        response = FakeResponse(b'{"choices":[{"message":{"content":"ok"}}]}')
        with patch.object(LMStudioProvider, 'models', return_value=['local-model']), patch('codex_ai_router.providers.lmstudio.urlopen', return_value=response) as open_:
            provider = LMStudioProvider(timeout=60, max_tokens=256); self.assertEqual(provider.ask('brief'), 'ok')
            payload = json.loads(open_.call_args.args[0].data)
            self.assertEqual((payload['max_tokens'], payload['stream']), (256, False))
    def test_90_openminis_discovery_contract_adapter(self):
        payload = json.dumps({'data': [{'id': value} for value in ['glm5.2', 'deepseekv4flash-0731', 'minimaxm3', 'grok-4.5', 'grok-4.6', 'grok-imagine-image-lite']]}).encode()
        metadata = {'model_discovery_endpoint': '/models', 'model_discovery_method': 'GET', 'model_discovery_auth_style': 'bearer', 'model_discovery_headers': {'User-Agent': 'DISCOVERY_UA'}, 'headers': {'X-Inference-Only': 'INFERENCE_ENV'}}
        with patch.dict(os.environ, {'DISCOVERY_KEY': 'test-key', 'DISCOVERY_UA': 'test-agent', 'INFERENCE_ENV': 'inference-only'}, clear=True), patch('codex_ai_router.providers.openai_compatible.urlopen', return_value=FakeResponse(payload)) as open_:
            provider = OpenAICompatibleProvider('https://example.test', key_env='DISCOVERY_KEY', requires_bearer_auth=False, provider_id='api1', provider_metadata=metadata, header_env=metadata['headers'])
            self.assertEqual(provider.models(), ['glm5.2', 'deepseekv4flash-0731', 'minimaxm3', 'grok-4.5', 'grok-4.6', 'grok-imagine-image-lite'])
            request = open_.call_args.args[0]
            self.assertEqual((request.get_method(), request.full_url), ('GET', 'https://example.test/v1/models'))
            self.assertIsNotNone(request.get_header('Authorization')); self.assertIsNotNone(request.get_header('User-agent'))
            self.assertIsNone(request.get_header('X-inference-only'))
    def test_91_discovery_auth_is_independent_from_inference_auth(self):
        metadata = {'model_discovery_auth_style': 'bearer', 'model_discovery_headers': {'X-Discovery': 'DISCOVERY_ENV'}}
        with patch.dict(os.environ, {'KEY_ENV': 'test-key', 'DISCOVERY_ENV': 'discovery', 'INFERENCE_ENV': 'inference'}, clear=True):
            provider = OpenAICompatibleProvider('https://example.test', key_env='KEY_ENV', requires_bearer_auth=False, provider_metadata=metadata, header_env={'X-Inference': 'INFERENCE_ENV'})
            self.assertIn('X-Inference', provider.request_headers())
            self.assertNotIn('X-Inference', provider.discovery_request_headers())
            self.assertIn('Authorization', provider.discovery_request_headers())
    def test_92_custom_discovery_endpoint_post_query_and_body(self):
        metadata = {'model_discovery_endpoint': '/catalog', 'model_discovery_method': 'POST', 'model_discovery_auth_style': 'none', 'model_discovery_query': {'region': 'test'}, 'model_discovery_body': {'scope': 'models'}}
        provider = OpenAICompatibleProvider('https://example.test/v1', requires_bearer_auth=False, provider_metadata=metadata)
        request = provider.discovery_request()
        self.assertEqual((request.get_method(), request.full_url), ('POST', 'https://example.test/v1/catalog?region=test'))
        self.assertEqual(json.loads(request.data), {'scope': 'models'})
    def test_93_discovery_only_mode_never_invokes_inference_validation(self):
        error = HTTPError('https://host/v1/models', 401, 'unauthorized', None, None)
        metadata = {'models': ['configured'], 'model_discovery_auth_style': 'none', 'model_discovery_validate_candidates': False}
        with patch('codex_ai_router.providers.openai_compatible.urlopen', side_effect=error) as open_:
            provider = OpenAICompatibleProvider('https://host', wire_api='responses', requires_bearer_auth=False, provider_metadata=metadata)
            self.assertEqual(provider.models(), [])
            self.assertEqual(open_.call_count, 1)
    def test_94_fast_local_gate_allows_only_simple_low_risk_work(self):
        from codex_ai_router.policy import FastLocalGate
        gate = FastLocalGate()
        self.assertTrue(gate.permits('summarize this short README', 'DOCS', Risk.LOW))
        self.assertFalse(gate.permits('edit multiple files and run tests', 'SMALL_CODE', Risk.MEDIUM))
        self.assertFalse(gate.permits('x' * 20000, 'DOCS', Risk.LOW))
    def test_95_medium_code_is_api_first(self):
        self.assertEqual(self.router().route('implement function')['mode'], 'API_ONLY')
    def test_96_local_structure_failure_falls_back_to_api_once(self):
        class BrokenLocal(FakeProvider):
            def ask(self, prompt): raise ProviderError('LOCAL_PROVIDER_ERROR:TimeoutError')
        local = BrokenLocal()
        router = Router(Path.cwd(), local, FakeProvider())
        result = router.delegate('summarize README')
        self.assertEqual(result.status, 'PASS'); self.assertIn('FALLBACK_TO_API', result.warnings)
    def test_97_performance_tracker_degrades_repeatedly_slow_models(self):
        from codex_ai_router.accounting.performance import PerformanceTracker
        with tempfile.TemporaryDirectory() as temp:
            tracker = PerformanceTracker(Path(temp) / 'performance.json')
            for _ in range(3): tracker.record('local:model', 21, True, True, 20)
            self.assertTrue(tracker.is_degraded('local:model'))
    def test_98_local_agent_actions_are_capped_at_two_steps(self):
        output = '{"status":"PASS","summary":"ok","confidence":1,"risk":"LOW","needs_escalation":false,"actions":["one","two","three"],"tests":[],"warnings":[]}'
        local = FakeProvider(output=output); router = Router(Path.cwd(), local, FakeProvider())
        result = router.delegate('summarize README', Mode.LOCAL_ONLY)
        self.assertEqual(len(result.actions), 2)
    def test_99_fast_local_budgets_load_from_config(self):
        from codex_ai_router.policy import FastLocalPolicy
        policy = FastLocalPolicy.from_config({'local': {'hard_timeout_seconds': 25, 'max_agent_steps': 2}})
        self.assertEqual((policy.hard_timeout_seconds, policy.max_agent_steps), (25, 2))

if __name__ == '__main__': unittest.main()

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
        self.assertTrue(ask_structured(FakeProvider(output='bad'), 'x', 'LOW').needs_escalation)
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
            provider = OpenAICompatibleProvider('https://unsupported', 'manual', requires_bearer_auth=False); self.assertEqual(provider.candidate_models(), ['manual'])
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

if __name__ == '__main__': unittest.main()

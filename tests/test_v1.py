import os
import tempfile
import time
import unittest
import subprocess
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


class FakeProvider:
    def __init__(self, available=True, output=None): self._available, self.output, self.calls = available, output or '{"status":"PASS","summary":"ok","confidence":0.9,"risk":"LOW","needs_escalation":false,"actions":[],"tests":[],"warnings":[]}', 0
    def available(self): return self._available
    def models(self): return ["fake"] if self._available else []
    def ask(self, prompt): self.calls += 1; return self.output


class RouterV1Tests(unittest.TestCase):
    def router(self, local=True, api=True): return Router(Path.cwd(), FakeProvider(local), FakeProvider(api))
    def test_01_local_model_discovery(self):
        with patch('codex_ai_router.providers.lmstudio.urlopen') as open_: open_.return_value.__enter__.return_value.read.return_value=b'{"data":[{"id":"model"}]}' ; self.assertEqual(LMStudioProvider().models(), ["model"])
    def test_02_api_missing_key(self):
        with patch.dict(os.environ, {}, clear=True): self.assertFalse(OpenAICompatibleProvider().available())
    def test_03_api_key_not_repr(self): self.assertNotIn('secret', repr(OpenAICompatibleProvider()))
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

if __name__ == '__main__': unittest.main()

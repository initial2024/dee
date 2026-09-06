import tempfile
from pathlib import Path
import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch
from codex_ai_router.codex_config_switcher import CodexConfigError, CodexConfigSwitcher
from codex_ai_router.cli import main

BASE='model_provider = "DEFAULT"\n[model_providers.DEFAULT]\nname = "Official"\napi_key = "fixture-redacted"\n'
class SwitcherTests(unittest.TestCase):
 def setUp(self): self.d=tempfile.TemporaryDirectory(); self.root=Path(self.d.name); self.c=self.root/'config.toml'; self.c.write_text(BASE,encoding='utf-8'); self.s=CodexConfigSwitcher(self.c,self.root)
 def tearDown(self): self.d.cleanup()
 def test_detect_backup_and_capture(self):
  summary = self.s.detect_config()
  self.assertEqual(summary['status'],'OFFICIAL_CODEX')
  self.assertNotIn('fixture-redacted', str(summary))
  self.assertEqual(self.s.backup_config()['status'],'BACKUP_CREATED')
  self.assertEqual(self.s.capture_official_profile(user_confirmed=True)['status'],'OFFICIAL_PROFILE_CAPTURED')
 def test_xiaoyu_and_restore(self): self.s.capture_official_profile(user_confirmed=True); r=self.s.switch_to_xiaoyu_router(); self.assertEqual(r['base_url'],'http://127.0.0.1:18789/v1'); self.assertEqual(r['wire_api'],'responses'); self.assertEqual(self.s.restore_previous_config()['status'],'PREVIOUS_CONFIG_RESTORED')
 def test_blocks_and_validates(self):
  with self.assertRaises(CodexConfigError): self.s.switch_to_xiaoyu_router('http://0.0.0.0:18789/v1')
  self.assertEqual(self.s.switch_to_official_codex()['status'],'OFFICIAL_PROFILE_NOT_CAPTURED')
  self.s.switch_to_xiaoyu_router()
  self.c.write_text(self.c.read_text(encoding='utf-8').replace('responses','chat_completions'),encoding='utf-8')
  self.assertIn('WIRE_API_INVALID',self.s.validate_config()['issues'])
  self.c.write_text(self.c.read_text(encoding='utf-8').replace('http://127.0.0.1:18789/v1','http://192.168.1.8:18789/v1'),encoding='utf-8')
  self.assertIn('NON_LOOPBACK_ENDPOINT_BLOCKED',self.s.validate_config()['issues'])
 def test_cli_status_uses_explicit_temporary_path(self):
  out = StringIO()
  with patch('sys.argv',['xiaoyu-router','codex-config','status','--config-path',str(self.c),'--root',str(self.root)]), redirect_stdout(out): main()
  self.assertIn('OFFICIAL_CODEX',out.getvalue())
  self.assertNotIn('fixture-redacted',out.getvalue())

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "outputs" / "SQA_MVP"))
from bug_audit_worker.config import ConfigError, doctor, load_config, canonical_url

EXAMPLE = '''
[worker]
profile = "dev"
data_dir = "data"
runs_dir = "runs"
allow_apply = false
[scope]
project_id = 1
project_name = "Synthetic"
team_ids = [2, 3]
team_names = ["A", "B"]
[tp]
base_url = "https://tp.example.invalid"
context_id = "configured-context"
token_env = "TEST_TP_TOKEN"
[model]
base_url = "https://model.example.invalid/v1"
api_style = "chat_completions"
name = "configured-model"
key_env = "TEST_MODEL_KEY"
[feishu]
base_url = "https://open.feishu.cn"
app_id = "configured-app"
secret_env = "TEST_FEISHU_SECRET"
chat_id = "configured-chat"
chat_name = "Synthetic"
bot_open_id = "configured-bot"
mentions = [{open_id="configured-user", name="Reviewer"}]
'''

class ConfigTests(unittest.TestCase):
    def test_equivalent_origin_and_default_port_normalization_keeps_path_case(self):
        self.assertEqual(canonical_url("https://TP.EXAMPLE.invalid:443/"), "https://tp.example.invalid")
        self.assertEqual(canonical_url("http://LOCALHOST:80/api/V2/"), "http://localhost/api/V2")
        self.assertEqual(canonical_url("http://[::1]:8800/"), "http://[::1]:8800")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="审计 config ")
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "worker.toml"

    def load(self, text=EXAMPLE):
        self.path.write_text(text, encoding="utf-8")
        return load_config(self.path)

    def test_relative_paths_follow_config_not_cwd(self):
        cfg = self.load()
        self.assertEqual(cfg.data_dir, self.path.parent / "data")
        self.assertEqual(cfg.runs_dir, self.path.parent / "runs")
        self.assertFalse(cfg.data_dir.exists())

    def test_offline_doctor_needs_no_secrets_and_creates_no_database(self):
        with patch.dict(os.environ, {}, clear=True):
            cfg = self.load()
            result = doctor(cfg)
        self.assertEqual(result["status"], "ready")
        self.assertFalse(cfg.data_dir.exists())
        self.assertNotIn(".codex", str(result))

    def test_credentials_are_loaded_only_explicitly_and_not_in_repr(self):
        cfg = self.load()
        with patch.dict(os.environ, {"TEST_TP_TOKEN": "secret-value"}):
            self.assertEqual(cfg.secret("tp"), "secret-value")
            self.assertNotIn("secret-value", repr(cfg))
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(ConfigError):
            cfg.secret("tp")

    def test_unknown_keys_literal_secrets_and_unsafe_urls_are_rejected(self):
        for text in (EXAMPLE + '\n[unknown]\nx=1\n', EXAMPLE.replace('token_env =', 'token ='),
                     EXAMPLE.replace('https://tp.example.invalid', 'https://user:password@tp.example.invalid'),
                     EXAMPLE.replace('https://tp.example.invalid', 'http://remote.example.invalid'),
                     EXAMPLE.replace('team_ids = [2, 3]', 'team_ids = []')):
            with self.subTest(text=text[-40:]), self.assertRaises(ConfigError):
                self.load(text)

    def test_missing_timezone_is_an_actionable_offline_failure(self):
        cfg = self.load()
        with patch("bug_audit_worker.config.ZoneInfo", side_effect=Exception("missing")):
            result = doctor(cfg)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("tzdata", str(result))

if __name__ == "__main__":
    unittest.main()

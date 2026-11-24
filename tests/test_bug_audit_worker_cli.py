import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / "outputs/SQA_MVP/bug_audit_worker_cli.py"


class CLITests(unittest.TestCase):
    def call(self, *args):
        return subprocess.run([sys.executable, "-X", "utf8", str(ENTRY), *args], capture_output=True, encoding="utf-8")

    def test_doctor_default_does_not_need_secrets(self):
        result = self.call("doctor")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(json.loads(result.stdout)["status"], "ready")

    def test_live_required_and_apply_is_explicit(self):
        result = self.call("run")
        self.assertEqual(result.returncode, 2)
        self.assertIn("live", result.stdout)

    def test_demo_offline_and_existing_business_directory_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "演示 数据"
            result = self.call("demo", "--data-dir", str(dest))
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            value = json.loads(result.stdout)
            self.assertEqual(value["mode"], "demo")
            self.assertTrue((dest / "pilot.sqlite3").exists())
            again = self.call("demo", "--data-dir", str(dest))
            self.assertEqual(again.returncode, 0, again.stdout)
            other = Path(tmp) / "business"; other.mkdir(); (other / "untouched").write_text("keep")
            self.assertEqual(self.call("demo", "--data-dir", str(other)).returncode, 2)
            self.assertEqual((other / "untouched").read_text(), "keep")

    def test_json_stdout_is_utf8_even_under_windows_legacy_encoding(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "PYTHONIOENCODING": "gbk"}
            result = subprocess.run([sys.executable, str(ENTRY), "demo", "--data-dir", str(Path(tmp) / "演示")], capture_output=True, env=env)
            self.assertEqual(result.returncode, 0)
            value = json.loads(result.stdout.decode("utf-8"))
            self.assertIn("合成演示", value["notice"])

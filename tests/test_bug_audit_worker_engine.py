import copy
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "outputs" / "SQA_MVP"))
from bug_audit_worker.config import ConfigError
from bug_audit_worker.engine import Worker
from bug_audit_worker.journal import JournalError
from tests.bug_audit_worker_server import WorkerServer
from tests.test_bug_audit_worker_tp import WINDOW


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.remote = WorkerServer().__enter__()
        self.addCleanup(self.remote.__exit__, None, None, None)
        self.remote.config.data["model"]["api_style"] = "responses"
        self.worker = Worker(self.remote.config)

    def test_dry_run_has_no_external_writes_and_freezes_first_window(self):
        result = self.worker.run("dry_run", WINDOW)
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["mode"], "dry_run")
        self.assertEqual(self.remote.counts[("POST", "/api/v1/Bugs/bulk")], 0)
        self.assertEqual(self.remote.counts[("POST", "/open-apis/im/v1/messages")], 0)
        later = {**WINDOW, "end": "2026-11-24T20:00:00+08:00"}
        replay = self.worker.run("dry_run", later)
        self.assertEqual(replay["run_id"], result["run_id"])
        self.assertEqual(replay["window"], result["window"])
        self.assertEqual(self.remote.counts[("POST", "/v1/responses")], 1)
        self.assertNotEqual(self.worker.audit.get_run(result["run_id"])["writeback_status"], "verified")

    def test_apply_complete_replay_and_reconcile_do_not_repeat_effects(self):
        result = self.worker.run("apply", WINDOW)
        self.assertEqual(result["exit_code"], 0, result)
        self.assertEqual(self.remote.counts[("POST", "/api/v1/Bugs/bulk")], 1)
        self.assertEqual(self.remote.counts[("POST", "/open-apis/im/v1/messages")], 2)
        self.assertEqual(self.worker.audit.get_run(result["run_id"])["status"], "complete")
        posts = {k: v for k, v in self.remote.counts.items() if k[0] == "POST" and "auth" not in k[1]}
        self.assertEqual(self.worker.resume(result["run_id"])["exit_code"], 0)
        self.assertEqual(self.worker.reconcile(result["run_id"], online=True)["exit_code"], 0)
        self.assertEqual(posts, {k: v for k, v in self.remote.counts.items() if k[0] == "POST" and "auth" not in k[1]})

    def test_apply_gate_and_profile_cannot_bypass_same_day_delivery(self):
        cfg = self.remote.config
        cfg.data["worker"]["allow_apply"] = False
        with self.assertRaises(ConfigError): self.worker.run("apply", WINDOW)
        self.assertFalse(self.remote.requests)
        cfg.data["worker"]["allow_apply"] = True
        first = self.worker.run("apply", WINDOW)
        other = copy.deepcopy(cfg)
        object.__setattr__(other, "profile", "another")
        other.data["worker"]["profile"] = "another"
        with self.assertRaises(ConfigError): Worker(other).run("apply", WINDOW)
        self.assertEqual(self.remote.counts[("POST", "/open-apis/im/v1/messages")], 2)
        self.assertEqual(first["exit_code"], 0)

    def test_partial_file_resume_only_retries_failed_delivery(self):
        self.remote.fail_file_send = True
        first = self.worker.run("apply", WINDOW)
        self.assertEqual(first["exit_code"], 3)
        self.remote.fail_file_send = False
        resumed = self.worker.resume(first["run_id"])
        self.assertEqual(resumed["exit_code"], 0, resumed)
        self.assertEqual(self.remote.counts[("POST", "/api/v1/Bugs/bulk")], 1)
        self.assertEqual(self.remote.counts[("POST", "/open-apis/im/v1/messages")], 3)
        self.assertEqual(self.remote.counts[("POST", "/v1/responses")], 1)

    def test_unknown_summary_needs_verified_operator_receipt(self):
        self.remote.lose_summary_response = True
        first = self.worker.run("apply", WINDOW)
        self.assertEqual(first["exit_code"], 3)
        self.worker.resume(first["run_id"])
        self.assertEqual(self.remote.counts[("POST", "/open-apis/im/v1/messages")], 1)
        mid = next(iter(self.remote.messages))
        key = str(first["run_id"]) + ":feishu:summary"
        self.worker.reconcile(first["run_id"], online=True, receipts={key: mid})
        self.remote.lose_summary_response = False
        self.assertEqual(self.worker.resume(first["run_id"])["exit_code"], 0)
        self.assertEqual(self.remote.counts[("POST", "/open-apis/im/v1/messages")], 2)

    def test_checkpoint_tampering_and_changed_config_are_blocked(self):
        first = self.worker.run("dry_run", WINDOW)
        job = self.worker.journal.job(first["run_id"])
        path = self.remote.config.runs_dir / job["checkpoint"]["path"]
        path.write_text("{}", encoding="utf-8")
        with self.assertRaises(JournalError): self.worker.resume(first["run_id"])

    def test_legacy_receipt_blocks_same_day_apply(self):
        old = self.worker.audit.begin_run("legacy", WINDOW, {"project": "Synthetic", "teams": ["A"]}, "2026-11-10")
        self.worker.audit.record_notification(old["id"], "old-sent", {"schema_version": 1, "status": "sent"})
        with self.assertRaises(ConfigError): self.worker.run("apply", WINDOW)

    def test_model_budget_survives_interrupted_review(self):
        self.remote.config.data["limits"]["max_model_calls"] = 1
        worker = Worker(self.remote.config)
        original = worker.model.review
        def crash(*args):
            original(*args)
            raise RuntimeError("模拟模型响应后进程中断")
        with patch.object(worker.model, "review", side_effect=crash):
            with self.assertRaises(RuntimeError): worker.run("dry_run", WINDOW)
        run_id = worker.audit.list_runs()[0]["id"]
        resumed = Worker(self.remote.config).resume(run_id)
        self.assertEqual(resumed["exit_code"], 3)
        self.assertEqual(self.remote.counts[("POST", "/v1/responses")], 1)

    def test_feature_lookup_rejects_unknown_or_cross_project_ids(self):
        from bug_audit_worker.tp import TPClient
        client = TPClient(self.remote.config)
        with self.assertRaises(ConfigError): client.feature_details([999], [])
        self.assertEqual(self.remote.counts[("GET", "/api/v2/Features")], 0)
        self.remote.features = [{"id": 25, "name": "Navigation", "description": "Route", "projectId": 2}]
        with self.assertRaises(ConfigError): client.feature_details([25], [{"id": 25, "projectId": 1}])
        self.remote.features[0]["projectId"] = 1
        self.assertEqual(client.feature_details([25], [{"id": 25, "projectId": 1}])[0]["description"], "Route")


if __name__ == "__main__": unittest.main()

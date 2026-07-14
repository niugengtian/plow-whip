import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import plow_whip.agent_flow as af
from plow_whip import health, supervisor


class HealthTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_environment_key_pool_is_safely_identified(self):
        refs = health.deepseek_keys({"DEEPSEEK_API_KEY": "secret-abcd", "DEEPSEEK_API_KEY_02": "other-abcd"})
        self.assertEqual(len(refs), 2)
        self.assertTrue(all(item["key_ref"].startswith("deepseek/") for item in refs))
        self.assertNotEqual(refs[0]["key_ref"], refs[1]["key_ref"])
        safe = health.safe_key_refs({"DEEPSEEK_API_KEY": "secret-abcd"})
        self.assertNotIn("secret", safe[0])

    def test_failure_classification_and_three_probe_recovery(self):
        self.assertEqual(health.classify_failure("429 insufficient_quota"), "auth_or_quota")
        self.assertEqual(health.classify_failure("socket hang up"), "network")
        self.assertEqual(health.classify_failure("verification failed"), "verification")
        health.open_circuit(self.tmp, "simple_tasker", "service", "503")
        self.assertTrue(health.is_open(self.tmp, "simple_tasker"))
        health.record_probe(self.tmp, "simple_tasker", True)
        health.record_probe(self.tmp, "simple_tasker", True)
        self.assertTrue(health.is_open(self.tmp, "simple_tasker"))
        health.record_probe(self.tmp, "simple_tasker", True)
        self.assertFalse(health.is_open(self.tmp, "simple_tasker"))


class SupervisorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.projects = os.path.join(self.tmp, "projects")
        self.config = os.path.join(self.tmp, "config")
        os.makedirs(self.projects)
        os.makedirs(self.config)
        self.old_file, self.old_dir = af.CONFIG_FILE, af.CONFIG_DIR
        af.CONFIG_DIR = self.config
        af.CONFIG_FILE = os.path.join(self.config, "config.json")
        af.save_config({"projects_dir": self.projects, "agents": ["codex", "codex_cli", "cursor_cli"]})
        for index in range(6):
            af.cmd_init(f"P{index}")

    def tearDown(self):
        af.CONFIG_FILE, af.CONFIG_DIR = self.old_file, self.old_dir
        shutil.rmtree(self.tmp)

    def test_per_driver_concurrency_is_capped_at_five(self):
        for index in range(6):
            state = af.load_state(f"P{index}")
            state["task"]["placeholder"] = False
            af.save_state(f"P{index}", state)

        def fake_spawn(project, state, driver):
            return {"project": project, "task_id": state["task"]["id"], "driver": driver, "pid": 1000 + int(project[1:]), "status": "started"}

        with patch("plow_whip.supervisor.reap_workers", return_value={"live": [], "finished": []}), \
             patch("plow_whip.supervisor.health.probe_open_circuits", return_value={}), \
             patch("plow_whip.supervisor.health.is_open", return_value=False), \
             patch("plow_whip.supervisor._spawn", side_effect=fake_spawn):
            result = supervisor.dispatch_projects([f"P{i}" for i in range(6)])
        statuses = [item["status"] for item in result["workers"]]
        self.assertEqual(statuses.count("started"), 5)
        self.assertEqual(statuses.count("queued_concurrency"), 1)

    def test_same_task_never_starts_a_second_worker(self):
        state = af.load_state("P0")
        state["task"]["placeholder"] = False
        af.save_state("P0", state)
        task_id = state["task"]["id"]
        live = [{"project": "P0", "task_id": task_id, "driver": "codex_cli", "pid": 999}]
        with patch("plow_whip.supervisor.reap_workers", return_value={"live": live, "finished": []}), \
             patch("plow_whip.supervisor.health.probe_open_circuits", return_value={}), \
             patch("plow_whip.supervisor.health.is_open", return_value=False), \
             patch("plow_whip.supervisor._pid_alive", return_value=True), \
             patch("plow_whip.supervisor._spawn") as spawn:
            result = supervisor.dispatch_projects(["P0"])
        self.assertEqual(result["workers"][0]["status"], "skipped_running")
        spawn.assert_not_called()

    def test_direct_cli_execution_blocks_the_scheduler_worker(self):
        state = af.load_state("P0")
        state["task"]["placeholder"] = False
        state["task"]["execution"] = {
            "dispatch_id": "DP-direct", "status": "running", "driver": "codex_cli", "cli_pid": 999,
        }
        af.save_state("P0", state)
        with patch("plow_whip.supervisor.reap_workers", return_value={"live": [], "finished": []}), \
             patch("plow_whip.supervisor.health.probe_open_circuits", return_value={}), \
             patch("plow_whip.supervisor.health.is_open", return_value=False), \
             patch("plow_whip.supervisor._pid_alive", return_value=True), \
             patch("plow_whip.supervisor._spawn") as spawn:
            result = supervisor.dispatch_projects(["P0"])
        self.assertEqual(result["workers"][0]["status"], "skipped_running")
        self.assertEqual(result["workers"][0]["dispatch_id"], "DP-direct")
        spawn.assert_not_called()

    def test_starting_claim_without_pid_blocks_a_competing_dispatch(self):
        state = af.load_state("P0")
        state["task"]["placeholder"] = False
        af.save_state("P0", state)
        task_id = state["task"]["id"]
        first = supervisor.claim_task("P0", task_id, "DP-one", "codex_cli", "codex_cli")
        second = supervisor.claim_task("P0", task_id, "DP-two", "codex_cli", "codex_cli")
        self.assertTrue(first["claimed"])
        self.assertFalse(second["claimed"])
        self.assertIn("already executing", second["detail"])

    def test_live_strict_worker_lease_is_renewed(self):
        state = af.load_state("P0")
        state["task"]["placeholder"] = False
        af.save_state("P0", state)
        claim = supervisor.claim_task("P0", state["task"]["id"], "DP-live", "codex_cli", "codex_cli")
        self.assertTrue(claim["claimed"])
        state = af.load_state("P0")
        state["task"]["execution"].update({"status": "running", "worker_pid": 999})
        state["task"]["execution"]["lease"]["expires_at"] = (datetime.now() + timedelta(seconds=30)).isoformat(timespec="seconds")
        af.save_state("P0", state)
        worker = {"project": "P0", "task_id": state["task"]["id"], "dispatch_id": "DP-live", "pid": 999}

        with patch("plow_whip.supervisor._pid_alive", return_value=True):
            renewed = supervisor.renew_live_leases([worker])

        lease = af.load_state("P0")["task"]["execution"]["lease"]
        self.assertEqual(len(renewed), 1)
        self.assertEqual(lease["renewals"], 1)
        self.assertGreater(datetime.fromisoformat(lease["expires_at"]), datetime.now() + timedelta(minutes=30))

    def test_strict_scheduler_rejects_zellij_worker(self):
        state = af.load_state("P0")
        state["task"].update({"placeholder": False, "requested_driver": "zellij"})
        af.save_state("P0", state)
        with patch("plow_whip.supervisor.reap_workers", return_value={"live": [], "finished": []}), \
             patch("plow_whip.supervisor.health.probe_open_circuits", return_value={}), \
             patch("plow_whip.supervisor._spawn") as spawn:
            result = supervisor.dispatch_projects(["P0"])
        self.assertEqual(result["workers"][0]["status"], "paused_unsupported_driver")
        spawn.assert_not_called()

    def test_branch_only_delivery_waits_then_detects_human_merge(self):
        state = af.load_state("P0")
        state["task"].update({"placeholder": False, "status": "done"})
        state["workflow"] = {
            "id": "T-delivery", "status": "delivery_ready", "target_branch": "main",
            "candidate_commit": "abc123", "git": {"branch": "plow/t-delivery", "target_branch": "main"},
            "delivery": {"status": "ready", "commit": "abc123", "target_branch": "main"},
        }
        af.save_state("P0", state)
        published = {
            "status": "awaiting_human_merge", "branch": "plow/t-delivery", "target_branch": "main",
            "commit": "abc123", "pushed": True, "merged": False,
        }
        with patch("plow_whip.supervisor.git_flow.publish_reviewed", return_value=published), \
             patch("plow_whip.supervisor.af.notify"):
            result = supervisor.reconcile_delivery("P0", af.load_state("P0"))
        self.assertEqual(result["status"], "awaiting_human_merge")
        self.assertEqual(af.load_state("P0")["workflow"]["status"], "awaiting_human_merge")
        with patch("plow_whip.supervisor.git_flow.remote_contains", return_value=True):
            result = supervisor.reconcile_delivery("P0", af.load_state("P0"))
        self.assertEqual(result["status"], "delivered")
        self.assertEqual(af.load_state("P0")["workflow"]["status"], "done")

    def test_human_merge_conflict_remains_reconcilable(self):
        state = af.load_state("P0")
        state["task"].update({"placeholder": False, "status": "awaiting_human_merge"})
        state["workflow"] = {
            "id": "T-conflict", "status": "awaiting_human_merge", "target_branch": "main",
            "candidate_commit": "abc123", "delivery": {
                "status": "awaiting_human_merge", "commit": "abc123", "target_branch": "main",
            },
        }
        af.save_state("P0", state)
        with patch("plow_whip.supervisor.git_flow.unexpected_control_changes", return_value=["app.py"]):
            self.assertIsNone(supervisor.quarantine_control_checkout("P0", af.load_state("P0")))
        self.assertEqual(af.load_state("P0")["workflow"]["status"], "awaiting_human_merge")
        with patch("plow_whip.supervisor.git_flow.remote_contains", return_value=True):
            result = supervisor.reconcile_delivery("P0", af.load_state("P0"))
        self.assertEqual(result["status"], "delivered")


if __name__ == "__main__":
    unittest.main()

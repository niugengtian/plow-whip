import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

import plow_whip.agent_flow as af
from plow_whip import codex_desktop, controller, supervisor


class ControllerReceiptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.projects = os.path.join(self.tmp, "projects")
        self.config = os.path.join(self.tmp, "config")
        os.makedirs(self.projects)
        os.makedirs(self.config)
        self.old_file, self.old_dir = af.CONFIG_FILE, af.CONFIG_DIR
        af.CONFIG_DIR = self.config
        af.CONFIG_FILE = os.path.join(self.config, "config.json")
        af.save_config({"projects_dir": self.projects, "agents": ["codex", "codex_cli", "pm"]})
        af.cmd_init("P")
        state = af.load_state("P")
        state["task"].update({
            "id": "T-1", "placeholder": False, "status": "active", "owner": "codex_cli",
            "execution": {
                "dispatch_id": "DP-1", "session_id": "S-1",
                "lease": {"id": "L-1", "generation": 1},
            },
        })
        af.save_state("P", state)

    def tearDown(self):
        af.CONFIG_FILE, af.CONFIG_DIR = self.old_file, self.old_dir
        shutil.rmtree(self.tmp)

    def _receipt(self):
        return controller.record_dispatch_result(
            "P", "T-1", "DP-1", success=True,
            result_ref=os.path.join(self.config, "runtime", "results", "DP-1.json"),
            execution_agent="codex_cli",
        )

    def test_receipt_is_idempotent_and_old_execution_becomes_stale(self):
        first = self._receipt()
        second = self._receipt()
        self.assertEqual(first["event_id"], second["event_id"])
        creates = [item for item in controller._read_events() if item.get("kind") == "receipt"]
        self.assertEqual(len(creates), 1)
        self.assertEqual(len(controller.pending("P")), 1)

        state = af.load_state("P")
        state["task"]["execution"] = {
            "dispatch_id": "DP-2", "lease": {"id": "L-2", "generation": 2},
        }
        af.save_state("P", state)

        self.assertEqual(controller.pending("P"), [])
        self.assertEqual(controller.states("P")[first["event_id"]]["status"], "stale")

    @patch("plow_whip.codex_desktop.authorize_control", return_value=True)
    @patch("plow_whip.codex_desktop.bound_control", return_value={"thread_ref": "sha256:controller"})
    def test_only_controller_consumption_closes_receipt(self, _bound, _authorized):
        receipt = self._receipt()
        consumed = controller.consume("P", receipt["event_id"])
        self.assertEqual(consumed["status"], "consumed")
        self.assertEqual(controller.pending("P"), [])
        self.assertEqual(controller.consume("P", receipt["event_id"])["status"], "consumed")

    @patch("plow_whip.controller._wake")
    @patch("plow_whip.codex_desktop.bound_control")
    def test_delivery_is_bounded_and_does_not_poll_in_one_turn(self, bound, wake):
        bound.return_value = {
            "thread_id": "controller-1", "thread_ref": "sha256:one", "role": "pm",
        }
        wake.return_value = {"accepted": False, "status": "busy"}
        receipt = self._receipt()
        created = datetime.fromisoformat(receipt["created_at"])

        controller.process_project("P", now=created)
        controller.process_project("P", now=created + timedelta(minutes=1))
        self.assertEqual(wake.call_count, 1)
        controller.process_project("P", now=created + timedelta(minutes=5))
        controller.process_project("P", now=created + timedelta(minutes=20))
        self.assertEqual(wake.call_count, 3)
        state = controller.states("P")[receipt["event_id"]]
        self.assertEqual(state["delivery_attempts"], 3)

    @patch("plow_whip.controller._new_controller")
    @patch("plow_whip.controller._wake")
    @patch("plow_whip.codex_desktop.bound_control")
    def test_controller_failover_resets_attempts_once(self, bound, wake, new_controller):
        bound.return_value = {
            "thread_id": "controller-1", "thread_ref": "sha256:one", "role": "pm",
        }
        new_controller.return_value = {
            "thread_id": "controller-2", "thread_ref": "sha256:two", "role": "pm",
        }
        wake.return_value = {"accepted": False, "status": "busy"}
        receipt = self._receipt()
        created = datetime.fromisoformat(receipt["created_at"])
        for minutes in (0, 5, 20):
            controller.process_project("P", now=created + timedelta(minutes=minutes))
        self.assertEqual(wake.call_count, 3)

        controller.process_project("P", now=created + timedelta(minutes=21))

        new_controller.assert_called_once()
        self.assertEqual(wake.call_count, 4)
        state = controller.states("P")[receipt["event_id"]]
        self.assertEqual(state["failovers"], 1)
        self.assertEqual(state["controller_ref"], "sha256:two")
        self.assertEqual(state["delivery_attempts"], 1)

    def test_start_pack_explicitly_forbids_controller_babysitting(self):
        self._receipt()
        pack = controller.start_pack("P")
        self.assertEqual(pack["mode"], "short-transaction")
        self.assertIn("Never wait or poll", pack["contract"])
        self.assertEqual(len(pack["pending"]), 1)
        self.assertIn("controller consume", pack["pending"][0]["consume"])

    def test_task_truth_completion_enqueues_receipt_before_worker_exit(self):
        with redirect_stdout(StringIO()):
            af.cmd_task("P", SimpleNamespace(
                action="complete", output="persisted", next=None,
                acceptance=None, verify=None, rule_tags=None, json=True,
                release_gate_report=None,
            ))
        receipts = list(controller.states("P").values())
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]["event_type"], "completion")
        self.assertEqual(receipts[0]["result_ref"], af.state_file("P"))

    def test_zero_token_worker_probe_reports_but_does_not_signal(self):
        log = os.path.join(self.tmp, "worker.log")
        with open(log, "w", encoding="utf-8") as file:
            file.write("still running")
        worker = {
            "project": "P", "task_id": "T-1", "dispatch_id": "DP-1",
            "agent": "codex_cli", "pid": 123, "cli_pid": 124,
            "log_file": log,
            "started_at": (datetime.now() - timedelta(minutes=10)).isoformat(timespec="seconds"),
        }
        with patch("plow_whip.supervisor._pid_alive", return_value=True), patch(
            "plow_whip.controller.record_health_incident",
            return_value={"event_id": "CR-health"},
        ) as incident:
            supervisor._probe_stalled_workers([worker], required_unchanged=1)
            alerts = supervisor._probe_stalled_workers([worker], required_unchanged=1)
        incident.assert_called_once()
        self.assertEqual(alerts[0]["event_id"], "CR-health")


class ControllerBindingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.projects = os.path.join(self.tmp, "projects")
        self.config = os.path.join(self.tmp, "config")
        self.codex_home = os.path.join(self.tmp, "codex")
        os.makedirs(self.projects)
        os.makedirs(self.config)
        self.old_file, self.old_dir = af.CONFIG_FILE, af.CONFIG_DIR
        af.CONFIG_DIR = self.config
        af.CONFIG_FILE = os.path.join(self.config, "config.json")
        af.save_config({
            "projects_dir": self.projects, "agents": ["codex", "pm", "qa"],
            "agent_meta": {
                "pm": {"roles": ["product-manager", "coordinator"], "driver": "file"},
                "qa": {"roles": ["qa"], "driver": "file"},
            },
        })
        af.cmd_init("P")

    def tearDown(self):
        af.CONFIG_FILE, af.CONFIG_DIR = self.old_file, self.old_dir
        shutil.rmtree(self.tmp)

    def _env(self, thread):
        return {
            "CODEX_HOME": self.codex_home,
            "CODEX_THREAD_ID": thread,
            "CODEX_INTERNAL_ORIGINATOR_OVERRIDE": "Codex Desktop",
        }

    def test_pm_role_rebinds_but_worker_role_does_not_replace_controller(self):
        with patch.dict(os.environ, self._env("human-current"), clear=True):
            fallback = codex_desktop.register_control_session("P", "qa")
        self.assertTrue(fallback["is_controller"])

        with patch.dict(os.environ, self._env("pm-fixed"), clear=True):
            pm = codex_desktop.register_control_session("P", "pm")
        self.assertTrue(pm["is_controller"])
        self.assertEqual(codex_desktop.bound_control("P")["thread_id"], "pm-fixed")

        with patch.dict(os.environ, self._env("qa-fixed"), clear=True):
            qa = codex_desktop.register_control_session("P", "qa")
        self.assertFalse(qa["is_controller"])
        self.assertEqual(codex_desktop.bound_control("P")["thread_id"], "pm-fixed")

    def test_controller_contract_remains_inside_startup_budget(self):
        view = {
            "mode": "short-transaction",
            "contract": "Dispatch atomically, persist awaiting_receipt, then end the turn. Never wait or poll child sessions.",
            "pending": [],
        }
        pack = af.build_start_pack("P", "pm", controller_view=view)
        self.assertEqual(pack["controller"]["mode"], "short-transaction")
        self.assertLessEqual(pack["budget"]["estimated_tokens"], pack["budget"]["hard_max"])


if __name__ == "__main__":
    unittest.main()

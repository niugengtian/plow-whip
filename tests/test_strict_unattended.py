"""Deterministic acceptance tests for the simplified unattended runtime."""

import json
import os
import shutil
import signal
import tempfile
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import plow_whip.agent_flow as af
from plow_whip import codex_desktop, dispatch, git_flow, leases, supervisor, tasking


class StrictUnattendedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.projects = os.path.join(self.tmp, "projects")
        self.config = os.path.join(self.tmp, "config")
        os.makedirs(self.projects)
        os.makedirs(self.config)
        self.old_config_dir = af.CONFIG_DIR
        self.old_config_file = af.CONFIG_FILE
        af.CONFIG_DIR = self.config
        af.CONFIG_FILE = os.path.join(self.config, "config.json")
        af.save_config({"projects_dir": self.projects, "agents": ["codex", "codex_cli", "cursor_cli"]})
        af.cmd_init("P")

    def tearDown(self):
        af.CONFIG_DIR = self.old_config_dir
        af.CONFIG_FILE = self.old_config_file
        shutil.rmtree(self.tmp)

    def test_reserved_hardening_is_disabled_by_default_but_code_remains(self):
        data = af.load_protocol("P")
        self.assertFalse(leases.hardening_enabled(data, "state_hmac"))
        self.assertFalse(leases.hardening_enabled(data, "protocol_authority_pin"))
        state = af.load_state("P")
        leases.sign_state(af.CONFIG_DIR, "P", state, data)
        self.assertNotIn("integrity", state)
        self.assertTrue(callable(leases.verify_state))
        self.assertTrue(callable(leases.verify_protocol))

    def test_terminal_first_bind_and_explicit_rebind_audit_both_refs(self):
        with patch.dict(os.environ, {"PLOW_WHIP_TERMINAL_ID": "tty-one"}, clear=True):
            first = codex_desktop.bind_control("P")
        with patch.dict(os.environ, {"PLOW_WHIP_TERMINAL_ID": "tty-two"}, clear=True):
            denied = codex_desktop.bind_control("P")
            rebound = codex_desktop.bind_control("P", rebind=True)
        self.assertTrue(first["bound"])
        self.assertFalse(denied["bound"])
        self.assertTrue(rebound["rebound"])
        checkpoint = codex_desktop._load_checkpoint("P")
        self.assertEqual(len(checkpoint["control_binding_history"]), 1)
        self.assertNotEqual(
            checkpoint["control_binding_history"][0]["ref"], checkpoint["control_binding"]["ref"]
        )

    def test_legacy_desktop_checkpoint_requires_explicit_rebind(self):
        path = codex_desktop._checkpoint_path("P")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"thread_id": "old-thread"}), encoding="utf-8")
        with patch.dict(os.environ, {"PLOW_WHIP_TERMINAL_ID": "new-terminal"}, clear=True):
            denied = codex_desktop.bind_control("P")
            allowed = codex_desktop.bind_control("P", rebind=True)
        self.assertFalse(denied["bound"])
        self.assertTrue(allowed["rebound"])

    def test_one_task_has_only_one_active_session(self):
        first = dispatch._record_cli_session("P", "codex_cli", "session-one")
        self.assertEqual(first["session_id"], "session-one")
        with self.assertRaisesRegex(RuntimeError, "already owns active session"):
            dispatch._record_cli_session("P", "cursor_cli", "session-two")
        state = af.load_state("P")
        self.assertEqual(state["task"]["active_session"]["agent"], "codex_cli")
        self.assertEqual(sum(bool(state["task"].get(key)) for key in ("active_session",)), 1)

    def test_revoked_worker_gets_term_then_kill_after_thirty_seconds(self):
        state = af.load_state("P")
        state["task"].update({"placeholder": False, "status": "active"})
        af.save_state("P", state)
        claim = supervisor.claim_task("P", "T-001", "DP-stop", "codex_cli", "codex_cli")
        state = af.load_state("P")
        state["task"]["execution"].update({"worker_pid": 101, "cli_pid": 102, "status": "running"})
        leases.revoke(state["task"], "replacement")
        af.save_state("P", state)
        worker = {"project": "P", "task_id": "T-001", "dispatch_id": "DP-stop", "pid": 101, "cli_pid": 102}
        with patch("plow_whip.supervisor._pid_alive", return_value=True), patch("os.killpg") as kill:
            first = supervisor._stop_revoked_workers([worker])
            self.assertEqual(first[0]["signal"], signal.SIGTERM.name)
            worker["stop_requested_at"] = (datetime.now() - timedelta(seconds=31)).isoformat(timespec="seconds")
            second = supervisor._stop_revoked_workers([worker])
            self.assertEqual(second[0]["signal"], signal.SIGKILL.name)
            self.assertTrue(kill.called)
        self.assertTrue(claim["claimed"])

    def test_replacement_waits_until_stopping_pid_is_gone(self):
        state = af.load_state("P")
        state["task"].update({"placeholder": False, "status": "active"})
        af.save_state("P", state)
        supervisor.claim_task("P", "T-001", "DP-old", "codex_cli", "codex_cli")
        state = af.load_state("P")
        state["task"]["execution"].update({"worker_pid": 555, "status": "running"})
        leases.revoke(state["task"], "replace")
        af.save_state("P", state)
        with patch("plow_whip.supervisor._pid_alive", return_value=True):
            blocked = supervisor.claim_task("P", "T-001", "DP-new", "codex_cli", "codex_cli")
        with patch("plow_whip.supervisor._pid_alive", return_value=False):
            claimed = supervisor.claim_task("P", "T-001", "DP-new", "codex_cli", "codex_cli")
        self.assertFalse(blocked["claimed"])
        self.assertTrue(claimed["claimed"])

    def test_dead_wrapper_does_not_reap_or_replace_live_cli(self):
        worker = {
            "project": "P", "task_id": "T-001", "dispatch_id": "DP-split",
            "pid": 700, "cli_pid": 701,
        }
        with patch.object(supervisor, "_load_registry", return_value={"workers": [worker]}), \
                patch.object(supervisor, "_save_registry"), \
                patch.object(supervisor, "_pid_alive", side_effect=lambda pid: int(pid or 0) == 701):
            reaped = supervisor.reap_workers()
        self.assertEqual(reaped["live"], [worker])
        self.assertEqual(reaped["finished"], [])

        with patch.object(supervisor, "_pid_alive", side_effect=lambda pid: int(pid or 0) == 701), \
                patch("os.killpg") as kill:
            self.assertTrue(supervisor._signal_worker_groups(worker, {}, signal.SIGTERM))
        kill.assert_called_once_with(701, signal.SIGTERM)

    def test_live_cli_renews_lease_after_wrapper_exit(self):
        state = af.load_state("P")
        state["task"].update({"placeholder": False, "status": "active"})
        af.save_state("P", state)
        supervisor.claim_task("P", "T-001", "DP-renew", "codex_cli", "codex_cli")
        state = af.load_state("P")
        execution = state["task"]["execution"]
        execution.update({"worker_pid": 800, "cli_pid": 801, "status": "running"})
        execution["lease"]["expires_at"] = (datetime.now() + timedelta(seconds=1)).isoformat(timespec="seconds")
        af.save_state("P", state)
        worker = {
            "project": "P", "task_id": "T-001", "dispatch_id": "DP-renew",
            "driver": "codex_cli", "pid": 800, "cli_pid": 801,
        }
        with patch.object(supervisor, "_pid_alive", side_effect=lambda pid: int(pid or 0) == 801):
            renewed = supervisor.renew_live_leases([worker])
        self.assertEqual(len(renewed), 1)
        self.assertEqual(renewed[0]["dispatch_id"], "DP-renew")

    def test_startup_and_recovery_budgets_are_bounded_english_json(self):
        state = af.load_state("P")
        state["task"]["next_action"] = "continue " * 1000
        state["task"]["last_output"] = "output " * 1000
        state["task"]["verify_commands"] = ["verify " * 1000 for _ in range(10)]
        af.save_state("P", state)
        pack = af.build_start_pack("P", "codex")
        encoded = json.dumps(pack, ensure_ascii=False)
        self.assertTrue(pack["ready"])
        self.assertLessEqual(pack["budget"]["estimated_tokens"], 600)
        self.assertLessEqual(len(pack["rules"]), 8)
        self.assertLessEqual(af.estimate_tokens(json.dumps(pack["recovery"])), 300)
        self.assertIn('"action"', encoded)

    def test_rotation_thresholds_and_carry_forward_are_token_aware(self):
        self.assertEqual(af.ROTATION_SOFT_TOKENS, 3000)
        self.assertEqual(af.ROTATION_HARD_TOKENS, 4000)
        self.assertEqual(af.ROTATION_FILE_SAFETY_BYTES, 16384)
        carry = af.generate_carry_forward("P", "codex", "- next " * 5000)
        self.assertLessEqual(af.estimate_tokens(carry), 300)

    def test_fixed_two_reviews_and_one_adjudication_cap(self):
        data = af.load_protocol("P")
        reviews = tasking._review_tasks(data, "T-X", "change", "codex_cli")
        self.assertEqual(len(reviews), 2)
        self.assertEqual([item["review_index"] for item in reviews], [1, 2])
        self.assertEqual(data["orchestration"]["max_adjudications"], 1)
        self.assertTrue(all(len(item["acceptance"]) == 4 for item in reviews))

    def test_each_passing_review_checks_exact_candidate_before_recording(self):
        state = af.load_state("P")
        state["workflow"] = {
            "id": "T-X", "status": "active", "code_change": True,
            "candidate_commit": "abc", "queue": [], "review_results": [],
        }
        review = {
            "id": "T-X-REVIEW-1", "owner": "codex_cli", "stage": "review",
            "status": "done", "last_output": "pass", "blockers": [],
        }
        with patch.object(af, "task_workspace", return_value="/tmp/candidate"), patch.object(
            git_flow, "assert_review_commit",
            side_effect=git_flow.GitFlowBlocked("reviewer changed task files: app.py"),
        ) as integrity:
            completed, action = tasking.advance_after_completion("P", state, review)
        integrity.assert_called_once_with("/tmp/candidate", "abc")
        self.assertEqual(action, "blocked")
        self.assertEqual(completed["blockers"], ["reviewer changed task files: app.py"])
        self.assertEqual(state["workflow"]["review_results"], [])

    def test_review_conflict_creates_exactly_one_pass_or_block_adjudication(self):
        state = af.load_state("P")
        implementation = {
            "id": "T-X", "title": "change", "owner": "codex_cli", "stage": "implementation",
            "status": "done", "next_action": "", "last_output": "built", "blockers": [],
            "acceptance": [], "verify_commands": [], "rule_tags": [], "decision_ids": [],
            "active_session": None, "session_archive": [],
        }
        review = {
            "id": "T-X-REVIEW-2", "title": "review", "owner": "codex_cli", "stage": "review",
            "status": "active", "next_action": "review", "last_output": "", "blockers": [],
            "acceptance": [], "verify_commands": [], "rule_tags": [], "decision_ids": [],
            "active_session": None, "session_archive": [],
        }
        state["workflow"] = {
            "id": "T-X", "title": "change", "status": "active", "queue": [],
            "candidate_commit": "abc", "last_implementation": implementation,
            "review_results": [{
                "task_id": "T-X-REVIEW-1", "reviewer": "cursor_cli", "result": "pass",
                "candidate_sha": "abc", "output": "pass",
            }],
        }
        state["task"] = review
        af.save_state("P", state)
        conflict = tasking.reject_review("P", "blocking issue")
        self.assertEqual(conflict["task"]["stage"], "adjudication")
        self.assertEqual(conflict["workflow"]["adjudications"], 1)
        blocked = tasking.reject_review("P", "adjudicator blocks")
        self.assertEqual(blocked["task"]["stage"], "implementation")
        self.assertTrue(blocked["workflow"]["needs_rereview"])

    def test_release_gate_runs_only_for_explicit_plow_whip_release_to_main(self):
        report = {
            "startup_tokens": 600, "recovery_tokens": 300,
            "reviewers": 2, "adjudications": 1,
            "github_e2e": {
                "repository": "niugengtian/plow-whip-e2e", "fixture": "stable-minimal-task",
                "success": True, "mutated_main": False,
                "implementation": "simple-tasker",
                "reviews": ["codex_cli", "cursor_cli"],
                "fallback_configured": True,
                "flow": git_flow.RELEASE_E2E_FLOW,
                "temporary_branches_deleted": True,
                "run_id": "e2e-test-run",
                "candidate_sha": "1" * 40,
                "main_sha_before": "2" * 40,
                "main_sha_after": "2" * 40,
                "target_branch": "e2e-target-test",
                "task_branch": "e2e-task-test",
            },
        }
        self.assertFalse(git_flow.release_gate_required(af.project_dir("P"), {"target_branch": "main"}))
        release_path = os.path.join(self.tmp, "plow-whip")
        self.assertTrue(git_flow.release_gate_required(release_path, {
            "target_branch": "main", "release_branch": True,
        }))
        git_flow.validate_release_gate(report)
        e2e = report["github_e2e"]
        evidence_ref = f"refs/tags/{git_flow.RELEASE_E2E_TAG_PREFIX}{e2e['run_id']}"
        remote_output = (
            f"{e2e['main_sha_before']}\trefs/heads/main\n"
            f"{e2e['candidate_sha']}\t{evidence_ref}\n"
        )
        with patch("subprocess.run", return_value=SimpleNamespace(
            returncode=0, stdout=remote_output, stderr="",
        )):
            git_flow.verify_github_e2e_remote(report)
        with patch("subprocess.run", return_value=SimpleNamespace(
            returncode=0,
            stdout=remote_output + f"{e2e['candidate_sha']}\trefs/heads/{e2e['task_branch']}\n",
            stderr="",
        )), self.assertRaisesRegex(git_flow.GitFlowBlocked, "still exists"):
            git_flow.verify_github_e2e_remote(report)
        with self.assertRaisesRegex(git_flow.GitFlowBlocked, "startup budget"):
            git_flow.validate_release_gate({**report, "startup_tokens": 601})

    def test_explicit_release_marker_reaches_prepared_git_state(self):
        workflow = {
            "id": "T-release", "target_branch": "main", "release_branch": True,
        }
        with patch.object(leases, "is_strict", return_value=True), patch.object(
            git_flow, "prepare_workspace",
            return_value={"branch": "plow/t-release", "target_branch": "main"},
        ):
            prepared = tasking._prepare_git("P", workflow)
        self.assertTrue(prepared["release_branch"])

    def test_controlled_progress_attaches_validated_release_evidence(self):
        report = {
            "startup_tokens": 500, "recovery_tokens": 200,
            "reviewers": 2, "adjudications": 0,
            "github_e2e": {
                "repository": git_flow.RELEASE_E2E_REPOSITORY,
                "fixture": git_flow.RELEASE_E2E_FIXTURE,
                "success": True, "mutated_main": False,
                "implementation": "simple-tasker",
                "reviews": ["codex_cli", "cursor_cli"],
                "fallback_configured": True,
                "flow": git_flow.RELEASE_E2E_FLOW,
                "temporary_branches_deleted": True,
                "run_id": "e2e-progress-test",
                "candidate_sha": "3" * 40,
                "main_sha_before": "4" * 40,
                "main_sha_after": "4" * 40,
                "target_branch": "e2e-target-progress",
                "task_branch": "e2e-task-progress",
            },
        }
        state = af.load_state("P")
        state["workflow"] = {
            "id": "T-release", "status": "active", "release_branch": True,
            "git": {"release_branch": True, "target_branch": "main"},
        }
        state["task"].update({"placeholder": False, "status": "active"})
        af.save_state("P", state)
        args = SimpleNamespace(
            action="progress", output="E2E complete", next="finish reviews",
            acceptance=None, verify=None, rule_tags=None, json=True,
            release_gate_report=json.dumps(report),
        )
        with patch("builtins.print"):
            af.cmd_task("P", args)
        stored = af.load_state("P")["workflow"]
        self.assertEqual(stored["git"]["release_gate_report"], report)


if __name__ == "__main__":
    unittest.main()

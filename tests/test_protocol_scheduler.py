"""Canonical protocol, startup payload, and native scheduler tests."""

import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

import plow_whip.agent_flow as af
from plow_whip import git_flow, leases, protocol, routing, scheduler, supervisor
from plow_whip.whip import run_once


class FakeArgs:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class ProtocolSchedulerTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.projects = os.path.join(self.tmpdir, "projects")
        self.config = os.path.join(self.tmpdir, "config")
        os.makedirs(self.projects)
        os.makedirs(self.config)
        self.old_file, self.old_dir = af.CONFIG_FILE, af.CONFIG_DIR
        af.CONFIG_DIR = self.config
        af.CONFIG_FILE = os.path.join(self.config, "config.json")
        af.save_config({"projects_dir": self.projects, "agents": ["codex", "codex_cli", "builder"]})
        af.cmd_init("P")

    def tearDown(self):
        af.CONFIG_FILE, af.CONFIG_DIR = self.old_file, self.old_dir
        shutil.rmtree(self.tmpdir)

    def _use_legacy_goal_workflow(self):
        """Move this isolated fixture to the compatibility mode under test."""
        data = af.load_protocol("P")
        paths = (
            leases._protocol_pin_path(af.CONFIG_DIR, "P", leases.protocol_epoch(data)),
            leases._legacy_protocol_pin_path(af.CONFIG_DIR, "P"),
        )
        for path in paths:
            if os.path.exists(path):
                os.unlink(path)
        data["enforcement"] = {"mode": "legacy"}
        protocol.save(af.project_dir("P"), data)

    def test_protocol_is_truth_and_handbook_is_derived(self):
        data = af.load_protocol("P")
        self.assertEqual(protocol.enabled_agents(data), ["codex", "codex_cli", "builder"])
        self.assertEqual(protocol.schedulable_agents(data), ["codex_cli", "builder"])
        self.assertIn("R004", protocol.effective_rules(data))
        with open(af.handbook_file("P"), encoding="utf-8") as f:
            self.assertIn("子智能体", f.read())

    def test_ensure_adds_new_global_rules_without_replacing_project_rules(self):
        data = af.load_protocol("P")
        data["global_rules"].pop("R007")
        data["global_rules"]["R005"]["summary"] = "legacy task/handoff-only wording"
        data["orchestration"].pop("retry_limit")
        data["project_rules"]["P001"] = {"summary": "local", "summary_zh": "本地", "locked": False}
        protocol.save(af.project_dir("P"), data)
        updated = protocol.ensure(af.project_dir("P"), "P", ["codex", "builder"])
        self.assertIn("R007", updated["global_rules"])
        self.assertIn("submit", updated["global_rules"]["R005"]["summary"])
        self.assertEqual(updated["orchestration"]["retry_limit"], 3)
        self.assertIn("P001", updated["project_rules"])

    def test_handbook_explains_latest_unattended_contract(self):
        data = af.load_protocol("P")
        data["agents"]["cursor"] = protocol.normalize_agent("cursor")
        data["agents"]["goal-planner"] = protocol.normalize_agent("goal-planner", {
            "roles": ["planner"], "capabilities": ["e2e-plan"], "driver": "codex_cli",
        })
        rendered = protocol.render_handbook(data)

        self.assertIn("## 无人值守闭环", rendered)
        self.assertIn("fast-forward", rendered)
        self.assertIn("每种 Driver 最大并发 | 5", rendered)
        self.assertIn("goal-planner` 仅保留兼容", rendered)
        self.assertIn("simple-tasker` 是内置按需 Agent", rendered)
        self.assertIn("health.json", rendered)
        self.assertIn("默认 Cursor Desktop", rendered)
        self.assertIn("观察身份", rendered)
        self.assertIn("旧 `goal start/plan` 只在 legacy 模式保留", rendered)
        self.assertEqual(data["agents"]["cursor"]["roles"], ["observer"])

    def test_ensure_migrates_legacy_agent_metadata_to_registry_v4(self):
        data = af.load_protocol("P")
        data["schema_version"] = 3
        data["agents"]["builder"] = {
            "role": "Backend", "assignment": "Build APIs", "enabled": True,
        }
        protocol.save(af.project_dir("P"), data)

        updated = protocol.ensure(af.project_dir("P"), "P", ["codex", "builder"])

        self.assertEqual(updated["schema_version"], 4)
        self.assertEqual(updated["agents"]["builder"]["roles"], ["backend"])
        self.assertEqual(updated["agents"]["builder"]["driver"], "file")
        self.assertEqual(updated["agents"]["codex"]["driver"], "control")
        self.assertFalse(updated["agents"]["codex"]["schedulable"])

    def test_future_protocol_schema_is_not_auto_repaired(self):
        data = af.load_protocol("P")
        data["schema_version"] = 5
        protocol.save(af.project_dir("P"), data)

        with self.assertRaisesRegex(ValueError, "unsupported future protocol schema"):
            protocol.ensure(af.project_dir("P"), "P", ["codex", "builder"])
        self.assertTrue(protocol.semantic_issues(data)[0].startswith("unsupported protocol schema"))

    def test_registry_normalizes_tags_and_rejects_unknown_drivers(self):
        agent = protocol.normalize_agent("api-worker", {
            "roles": ["Backend API", "backend_api"],
            "capabilities": ["Python", "API"],
            "driver": "codex_cli",
        })
        self.assertEqual(agent["roles"], ["backend-api"])
        self.assertEqual(agent["capabilities"], ["python", "api"])
        with self.assertRaisesRegex(ValueError, "unsupported driver"):
            protocol.normalize_agent("bad", {"driver": "magic"})

    def test_start_pack_is_bounded_and_complete(self):
        pack = af.build_start_pack("P", "codex")
        self.assertTrue(pack["ready"])
        self.assertEqual(pack["auth"]["mode"], "observer")
        self.assertFalse(pack["auth"]["execution_allowed"])
        self.assertNotIn("commands", pack)
        self.assertEqual(pack["task"]["id"], "T-001")
        self.assertIn("R001", [r["id"] for r in pack["rules"]])
        self.assertLessEqual(len(pack["rules"]), 8)
        for rule in pack["rules"]:
            self.assertEqual(set(rule), {"id", "action", "on_violation"})
        self.assertLessEqual(pack["budget"]["estimated_tokens"], af.START_PACK_NORMAL_TOKENS)

    def test_scheduler_lease_turns_exact_owner_into_worker(self):
        state = af.load_state("P")
        state["task"]["placeholder"] = False
        af.save_state("P", state)
        claim = supervisor.claim_task("P", "T-001", "DP-test", "codex_cli", "codex_cli")
        self.assertTrue(claim["claimed"])
        self.assertNotIn(claim["lease_token"], json.dumps(af.load_state("P"), ensure_ascii=False))
        with patch.dict(os.environ, {leases.TOKEN_ENV: claim["lease_token"]}):
            pack = af.build_start_pack("P", "codex_cli")
            self.assertEqual(pack["auth"]["mode"], "worker")
            self.assertTrue(pack["auth"]["execution_allowed"])
            self.assertIn("commands", pack)
            af._require_machine_lease("P", FakeArgs(command="task", action="progress"))
            with self.assertRaisesRegex(leases.LeaseDenied, "human control"):
                af._require_machine_lease("P", FakeArgs(command="plan", action="confirm"))

    def test_unleased_machine_write_is_denied_but_human_confirmation_is_allowed(self):
        with self.assertRaisesRegex(leases.LeaseDenied, "observer-only"):
            af._require_machine_lease("P", FakeArgs(command="task", action="complete"))
        with patch("plow_whip.codex_desktop.authorize_control", return_value=True):
            af._require_machine_lease("P", FakeArgs(command="plan", action="confirm"))
        with self.assertRaisesRegex(leases.LeaseDenied, "not an execution entry"):
            af._require_machine_lease("P", FakeArgs(command="task", action="start"))

    def test_project_agent_set_passes_through_human_control_gate(self):
        before = af.load_protocol("P")["agents"]["builder"]["driver"]
        argv = [
            "plow-whip", "--project", "P", "agent", "set", "builder",
            "--driver", "codex_cli",
        ]
        with patch.object(sys, "argv", argv), \
             patch("plow_whip.codex_desktop.authorize_control", return_value=False), \
             self.assertRaisesRegex(SystemExit, "3"):
            af.main()
        self.assertEqual(af.load_protocol("P")["agents"]["builder"]["driver"], before)
        with patch.object(sys, "argv", argv), \
             patch("plow_whip.codex_desktop.authorize_control", return_value=True):
            af.main()
        self.assertEqual(af.load_protocol("P")["agents"]["builder"]["driver"], "codex_cli")

    def test_worker_cannot_unset_lease_to_approve_its_own_plan(self):
        with patch.dict(os.environ, {
            "CODEX_INTERNAL_ORIGINATOR_OVERRIDE": "Codex CLI",
            "CODEX_THREAD_ID": "worker-thread",
        }, clear=True):
            with self.assertRaisesRegex(leases.LeaseDenied, "current bound Codex Desktop"):
                af._require_machine_lease("P", FakeArgs(command="plan", action="confirm"))

    def test_bootstrap_and_doctor_do_not_require_a_protocol(self):
        af._require_machine_lease("Fresh", FakeArgs(command="init", action=None))
        af._require_machine_lease("Fresh", FakeArgs(command="new", action=None))
        af._require_machine_lease("Fresh", FakeArgs(command="repair", action=None))
        af._require_machine_lease("Fresh", FakeArgs(command="doctor", action=None))
        af._require_machine_lease("Fresh", FakeArgs(command="doctor", action=None, repair=True))

    def test_partial_existing_state_cannot_use_bootstrap_exemption(self):
        os.makedirs(af.project_collab_dir("Partial"), exist_ok=True)
        with open(af.state_file("Partial"), "w", encoding="utf-8") as file:
            file.write("{}")
        for command in ("init", "new", "repair"):
            with self.subTest(command=command), \
                 self.assertRaisesRegex(leases.LeaseDenied, "without an authority protocol"):
                af._require_machine_lease("Partial", FakeArgs(command=command, action=None))

    def test_existing_strict_project_new_requires_human_control(self):
        before = af.load_state("P")["task"]["next_action"]
        argv = [
            "plow-whip", "--project", "P", "new",
            "--first-action", "replace canonical task",
        ]
        with patch.object(sys, "argv", argv), \
             patch("plow_whip.codex_desktop.authorize_control", return_value=False), \
             self.assertRaisesRegex(SystemExit, "3"):
            af.main()
        self.assertEqual(af.load_state("P")["task"]["next_action"], before)

    def test_rotation_enforcement_aliases_require_human_control(self):
        actions = (
            FakeArgs(command="rotation-health", action=None, enforce=True),
            FakeArgs(command="memory-budget", action=None, enforce_rotate=True),
        )
        with patch("plow_whip.codex_desktop.authorize_control", return_value=False):
            for args in actions:
                with self.subTest(command=args.command), \
                     self.assertRaisesRegex(leases.LeaseDenied, "current bound Codex Desktop"):
                    af._require_machine_lease("P", args)
        with patch("plow_whip.codex_desktop.authorize_control", return_value=True):
            for args in actions:
                af._require_machine_lease("P", args)

    def test_doctor_repair_requires_human_control_for_strict_project(self):
        with patch.dict(os.environ, {"CODEX_INTERNAL_ORIGINATOR_OVERRIDE": "Codex CLI"}, clear=True):
            with self.assertRaisesRegex(leases.LeaseDenied, "current bound Codex Desktop"):
                af._require_machine_lease("P", FakeArgs(command="doctor", action=None, repair=True))

    def test_revoked_lease_cannot_be_replayed(self):
        state = af.load_state("P")
        state["task"]["placeholder"] = False
        af.save_state("P", state)
        first = supervisor.claim_task("P", "T-001", "DP-one", "codex_cli", "codex_cli")
        state = af.load_state("P")
        leases.revoke(state["task"], "test handoff")
        af.save_state("P", state)
        second = supervisor.claim_task("P", "T-001", "DP-two", "codex_cli", "codex_cli")
        self.assertTrue(second["claimed"])
        with self.assertRaisesRegex(leases.LeaseDenied, "does not match|no longer active"):
            leases.validate(
                af.CONFIG_DIR, "P", af.load_state("P"), af.load_protocol("P"),
                token=first["lease_token"], agent="codex_cli",
            )

    def test_signed_state_can_renew_a_live_token_beyond_original_expiry(self):
        state = af.load_state("P")
        state["task"]["placeholder"] = False
        af.save_state("P", state)
        claim = supervisor.claim_task("P", "T-001", "DP-renew", "codex_cli", "codex_cli")
        payload = leases.decode(af.CONFIG_DIR, claim["lease_token"])
        future = payload["expires_at"] + 120
        state = af.load_state("P")
        state["task"]["execution"]["lease"]["expires_at"] = datetime.fromtimestamp(future + 60).isoformat(timespec="seconds")
        af.save_state("P", state)

        with patch("plow_whip.leases.time.time", return_value=future):
            validated = leases.validate(
                af.CONFIG_DIR, "P", af.load_state("P"), af.load_protocol("P"),
                token=claim["lease_token"], agent="codex_cli",
            )
        self.assertEqual(validated["dispatch_id"], "DP-renew")

    def test_expired_signed_lease_metadata_is_rejected(self):
        state = af.load_state("P")
        state["task"]["placeholder"] = False
        af.save_state("P", state)
        claim = supervisor.claim_task("P", "T-001", "DP-expired", "codex_cli", "codex_cli")
        state = af.load_state("P")
        state["task"]["execution"]["lease"]["expires_at"] = (datetime.now() - timedelta(seconds=1)).isoformat(timespec="seconds")
        af.save_state("P", state)

        with self.assertRaisesRegex(leases.LeaseDenied, "no longer active"):
            leases.validate(
                af.CONFIG_DIR, "P", af.load_state("P"), af.load_protocol("P"),
                token=claim["lease_token"], agent="codex_cli",
            )

    def test_protocol_authority_pin_rejects_strict_mode_downgrade(self):
        data = af.load_protocol("P")
        data["enforcement"]["reserved_hardening"]["protocol_authority_pin"] = True
        protocol.save(af.project_dir("P"), data)
        leases.pin_protocol(af.CONFIG_DIR, "P", data, new_incarnation=True)
        data.pop("enforcement")
        protocol.save(af.project_dir("P"), data)

        with self.assertRaisesRegex(leases.ProtocolIntegrityError, "enforcement changed"):
            af.load_protocol("P")
        with self.assertRaises(leases.ProtocolIntegrityError):
            af.load_state("P")
        self.assertIn("authority", af.build_doctor_report("P")["issues"][0])

    def test_legacy_authority_pin_is_migrated_without_losing_enforcement(self):
        data = af.load_protocol("P")
        data["enforcement"]["reserved_hardening"]["protocol_authority_pin"] = True
        protocol.save(af.project_dir("P"), data)
        leases.pin_protocol(af.CONFIG_DIR, "P", data, new_incarnation=True)
        current = leases._protocol_pin_path(af.CONFIG_DIR, "P", leases.protocol_epoch(data))
        legacy = leases._legacy_protocol_pin_path(af.CONFIG_DIR, "P")
        os.replace(current, legacy)

        self.assertEqual(af.load_protocol("P")["enforcement"]["mode"], "strict")
        self.assertTrue(os.path.exists(current))

    def test_archived_project_name_can_start_a_new_incarnation(self):
        old_epoch = leases.protocol_epoch(af.load_protocol("P"))
        state = af.load_state("P")
        state["task"]["status"] = "done"
        af.save_state("P", state)
        af.cmd_archive("P")

        af.cmd_init("P")

        self.assertNotEqual(leases.protocol_epoch(af.load_protocol("P")), old_epoch)

    def test_start_pack_clamps_unbounded_task_fields(self):
        state = af.load_state("P")
        state["task"]["title"] = "T" * 10000
        state["task"]["goal"] = "G" * 10000
        state["task"]["next_action"] = "N" * 10000
        state["task"]["acceptance"] = ["A" * 5000 for _ in range(100)]
        af.save_state("P", state)

        pack = af.build_start_pack("P", "codex")

        self.assertTrue(pack["ready"])
        self.assertLessEqual(len(pack["task"]["title"]), 96)
        self.assertLessEqual(len(pack["task"]["next"]), 196)
        self.assertLessEqual(len(pack["task"]["acceptance"]), 2)
        self.assertLessEqual(pack["budget"]["estimated_tokens"], af.START_PACK_NORMAL_TOKENS)

    def test_planning_catalog_size_does_not_grow_with_agent_count(self):
        data = af.load_protocol("P")
        for index in range(100):
            data["agents"][f"worker-{index}"] = protocol.normalize_agent(f"worker-{index}", {
                "roles": ["implementation"], "capabilities": ["python"], "driver": "file",
            })
        catalog = routing.planner_catalog(data)

        self.assertNotIn("agents", catalog)
        self.assertEqual(catalog["roles"].count("implementation"), 1)
        self.assertEqual(catalog["capabilities"].count("python"), 1)

    def test_important_rules_are_scoped_to_agent_and_request_details_on_demand(self):
        data = af.load_protocol("P")
        data["project_rules"]["P-agent"] = {
            "summary": "Builder-only deployment rule", "priority": "important",
            "applies_to": {"agents": ["builder"], "task_tags": ["deploy"]},
            "details_ref": "AGENT_PROTOCOL.json#P-agent",
        }
        protocol.save(af.project_dir("P"), data)
        protocol.ensure(af.project_dir("P"), "P", ["codex", "builder"])
        codex = af.build_start_pack("P", "codex")
        builder_without_tag = af.build_start_pack("P", "builder")
        state = af.load_state("P")
        state["task"]["rule_tags"] = ["deploy"]
        af.save_state("P", state)
        builder = af.build_start_pack("P", "builder")
        self.assertNotIn("P-agent", [r["id"] for r in codex["rules"]])
        self.assertNotIn("P-agent", [r["id"] for r in builder_without_tag["rules"]])
        self.assertIn("P-agent", [r["id"] for r in builder["rules"]])

    def test_rule_compilation_uses_untruncated_task_tags(self):
        data = af.load_protocol("P")
        data["project_rules"]["P-late"] = {
            "summary": "Late tag rule", "priority": "important",
            "applies_to": {"agents": ["codex"], "task_tags": ["late-tag"]},
        }
        protocol.save(af.project_dir("P"), data)
        protocol.ensure(af.project_dir("P"), "P", ["codex", "builder"])
        state = af.load_state("P")
        state["task"]["rule_tags"] = [f"tag-{index}" for index in range(20)] + ["late-tag"]
        af.save_state("P", state)

        pack = af.build_start_pack("P", "codex")

        self.assertIn("P-late", [rule["id"] for rule in pack["rules"]])
        self.assertNotIn("rule_tags", pack["task"])

    def test_task_command_updates_single_runtime_truth(self):
        af.cmd_task("P", FakeArgs(action="start", task_id="T-9", title="Build", owner="builder", next="Code", decisions=["D-1"], json=False))
        af.cmd_task("P", FakeArgs(
            action="progress", output="Half", next="Test",
            acceptance=["API works"], verify=["python -m unittest"], json=False,
        ))
        state = af.load_state("P")
        self.assertEqual(state["task"]["id"], "T-9")
        self.assertEqual(state["task"]["owner"], "builder")
        self.assertEqual(state["next_action"], "Test")
        self.assertEqual(state["task"]["acceptance"], ["API works"])
        self.assertEqual(state["task"]["verify_commands"], ["python -m unittest"])
        with open(af.state_file("P"), encoding="utf-8") as f:
            persisted = json.load(f)
        self.assertNotIn("project_path", persisted["task_context"])
        self.assertNotIn("agents", persisted)

    def test_task_completion_runs_acceptance_and_only_marks_done_on_success(self):
        af.cmd_task("P", FakeArgs(
            action="start", task_id="T-verify", title="Build", goal="Ship working code",
            owner="builder", next="Implement", acceptance=["tests pass"],
            verify=["python -m unittest"], decisions=[], json=False,
        ))
        with patch("plow_whip.agent_flow.subprocess.run", return_value=Mock(
            returncode=0, stdout="OK", stderr="",
        )) as run:
            af.cmd_task("P", FakeArgs(action="complete", output="Done", next=None, json=False))
        task = af.load_state("P")["task"]
        self.assertEqual(task["status"], "done")
        self.assertEqual(task["goal"], "Ship working code")
        self.assertEqual(task["acceptance"], ["tests pass"])
        self.assertEqual(task["verification"][0]["returncode"], 0)
        run.assert_called_once()

    def test_failed_acceptance_keeps_task_active_for_automatic_retry(self):
        af.cmd_task("P", FakeArgs(
            action="start", task_id="T-fail", title="Build", goal=None,
            owner="builder", next="Implement", acceptance=[],
            verify=["false"], decisions=[], json=False,
        ))
        with patch("plow_whip.agent_flow.subprocess.run", return_value=Mock(
            returncode=1, stdout="", stderr="broken",
        )):
            af.cmd_task("P", FakeArgs(action="complete", output="Done", next=None, json=False))
        task = af.load_state("P")["task"]
        self.assertEqual(task["status"], "active")
        self.assertEqual(task["next_action"], "Fix verification failure: false")
        self.assertIn("broken", task["last_output"])

    def _save_git_delivery_blocker(self):
        data = af.load_protocol("P")
        data.pop("enforcement", None)
        protocol.save(af.project_dir("P"), data)
        state = af.load_state("P")
        state["workflow"] = {
            "id": "T-delivery", "status": "blocked_waiting_human", "code_change": True,
            "queue": [], "git": {"branch": "plow/t-delivery", "target_branch": "main"},
            "completed": [{"id": "T-delivery-REVIEW", "title": "Review"}],
        }
        state["task"] = {
            "id": "T-delivery-REVIEW", "title": "Review", "owner": "codex_cli",
            "status": "blocked_waiting_human", "stage": "review",
            "next_action": "Resolve Git fast-forward blocker", "acceptance": [],
            "verify_commands": [], "rule_tags": [], "last_output": "reviewed",
            "blockers": ["origin/main moved and cannot fast-forward"], "decision_ids": [],
            "cli_sessions": {},
        }
        af.save_state("P", state)

    def test_git_delivery_blocker_retries_and_completes(self):
        self._save_git_delivery_blocker()
        delivery = {"branch": "plow/t-delivery", "target_branch": "main", "commit": "abc", "pushed": True, "merged": True}
        with patch("plow_whip.tasking.git_flow.finalize_fast_forward", return_value=delivery) as finalize:
            af.cmd_task("P", FakeArgs(action="complete", output="retry delivery", next=None, json=False))
        state = af.load_state("P")
        self.assertEqual(state["workflow"]["status"], "done")
        self.assertEqual(state["task"]["status"], "done")
        self.assertEqual(state["workflow"]["delivery"], delivery)
        self.assertEqual(len(state["workflow"]["completed"]), 1)
        finalize.assert_called_once()

    def test_git_delivery_blocker_stays_blocked_when_retry_fails(self):
        self._save_git_delivery_blocker()
        with patch(
            "plow_whip.tasking.git_flow.finalize_fast_forward",
            side_effect=git_flow.GitFlowBlocked("latest fast-forward blocker"),
        ):
            af.cmd_task("P", FakeArgs(action="complete", output="retry delivery", next=None, json=False))
        state = af.load_state("P")
        self.assertEqual(state["workflow"]["status"], "blocked_waiting_human")
        self.assertEqual(state["task"]["status"], "blocked_waiting_human")
        self.assertEqual(state["task"]["blockers"], ["latest fast-forward blocker"])

    def test_git_delivery_retry_survives_verification_failure_then_delivers(self):
        self._save_git_delivery_blocker()
        state = af.load_state("P")
        state["task"]["verify_commands"] = ["verify-delivery-fix"]
        af.save_state("P", state)
        delivery = {
            "branch": "plow/t-delivery", "target_branch": "main", "commit": "abc",
            "pushed": True, "merged": True,
        }

        with patch("plow_whip.agent_flow.subprocess.run", return_value=Mock(
            returncode=1, stdout="", stderr="still broken",
        )), patch("plow_whip.tasking.git_flow.finalize_fast_forward") as finalize:
            af.cmd_task("P", FakeArgs(action="complete", output="first retry", next=None, json=False))

        state = af.load_state("P")
        self.assertEqual(state["workflow"]["status"], "active")
        self.assertEqual(state["task"]["status"], "active")
        self.assertEqual(state["task"]["next_action"], "Fix verification failure: verify-delivery-fix")
        finalize.assert_not_called()

        with patch("plow_whip.agent_flow.subprocess.run", return_value=Mock(
            returncode=0, stdout="fixed", stderr="",
        )), patch(
            "plow_whip.tasking.git_flow.finalize_fast_forward", return_value=delivery,
        ) as finalize:
            af.cmd_task("P", FakeArgs(action="complete", output="fixed and verified", next=None, json=False))

        state = af.load_state("P")
        self.assertEqual(state["workflow"]["status"], "done")
        self.assertEqual(state["task"]["status"], "done")
        self.assertEqual(state["workflow"]["delivery"], delivery)
        self.assertEqual(len(state["workflow"]["completed"]), 1)
        finalize.assert_called_once()

    def test_plan_confirmation_is_not_released_by_task_complete(self):
        state = af.load_state("P")
        state["workflow"] = {
            "id": "T-plan", "status": "awaiting_confirmation", "code_change": True,
            "plan": [{"title": "Build"}],
        }
        state["task"].update({
            "status": "blocked_waiting_human", "stage": "planning",
            "next_action": "Wait for human plan confirmation",
            "blockers": ["plan_confirmation_required"],
        })
        af.save_state("P", state)
        with patch("plow_whip.tasking.git_flow.finalize_fast_forward") as finalize:
            af.cmd_task("P", FakeArgs(action="complete", output="must not resume", next=None, json=False))
        state = af.load_state("P")
        self.assertEqual(state["workflow"]["status"], "awaiting_confirmation")
        self.assertEqual(state["task"]["status"], "blocked_waiting_human")
        self.assertEqual(state["task"]["blockers"], ["plan_confirmation_required"])
        finalize.assert_not_called()

    def test_goal_plan_advances_coarse_milestones_and_finishes_only_at_end(self):
        self._use_legacy_goal_workflow()
        data = af.load_protocol("P")
        data["agents"]["builder"] = protocol.normalize_agent("builder", {
            "roles": ["implementation"], "driver": "cursor_cli",
        })
        protocol.save(af.project_dir("P"), data)
        af.cmd_goal("P", FakeArgs(action="start", text="Ship feature", owner="codex_cli"))
        state = af.load_state("P")
        self.assertEqual(state["goal"]["status"], "planning")
        plan = [
            {"title": "Build and test feature", "owner": "builder", "acceptance": ["unit tests pass"]},
            {"title": "Final integration acceptance", "owner": "codex_cli", "acceptance": ["full suite passes"], "final_acceptance": True},
        ]
        af.cmd_goal("P", FakeArgs(action="plan", context_summary="Reusable compact context", plan_json=json.dumps(plan)))
        state = af.load_state("P")
        self.assertEqual(state["task"]["owner"], "builder")
        self.assertEqual(state["task"]["goal_id"], state["goal"]["id"])
        self.assertNotIn("goal", state["task"])
        self.assertNotIn("goal", state["goal"]["queue"][0])
        self.assertEqual(state["goal"]["total"], 2)
        af.cmd_task("P", FakeArgs(action="complete", output="Feature done", next=None, json=False))
        state = af.load_state("P")
        self.assertEqual(state["task"]["title"], "Final integration acceptance")
        self.assertEqual(state["current_agent"], "codex_cli")
        self.assertEqual(state["goal"]["status"], "active")
        self.assertEqual(len(state["goal"]["completed"]), 1)
        af.cmd_task("P", FakeArgs(action="complete", output="Integrated", next=None, json=False))
        state = af.load_state("P")
        self.assertEqual(state["goal"]["status"], "done")
        self.assertEqual(state["task"]["status"], "done")

    def test_strict_mode_rejects_legacy_goal_workflow(self):
        with self.assertRaisesRegex(ValueError, "strict projects.*use submit"):
            af.cmd_goal("P", FakeArgs(
                action="start", text="Ship feature", owner="codex_cli", replace=False,
            ))

    def test_goal_compatibility_path_uses_configured_default_planner(self):
        self._use_legacy_goal_workflow()
        data = af.load_protocol("P")
        data["agents"]["cursor_cli"] = {"role": "Cursor CLI", "assignment": "", "enabled": True}
        data["agents"]["codex_cli"] = {"role": "Codex CLI", "assignment": "", "enabled": True}
        protocol.save(af.project_dir("P"), data)
        af.cmd_goal("P", FakeArgs(action="start", text="Ship feature", owner=None))
        self.assertEqual(af.load_state("P")["task"]["owner"], "codex_cli")
        next_action = af.load_state("P")["task"]["next_action"]
        self.assertIn("do not scan the repository", next_action)
        self.assertIn("run tests during planning", next_action)

    def test_goal_routes_roles_and_capabilities_without_agent_names(self):
        self._use_legacy_goal_workflow()
        data = af.load_protocol("P")
        data["agents"]["builder"] = protocol.normalize_agent("builder", {
            "role": "Backend", "roles": ["backend"], "capabilities": ["python", "api"],
            "driver": "cursor_cli", "priority": 80,
        })
        data["agents"]["auditor"] = protocol.normalize_agent("auditor", {
            "role": "Reviewer", "roles": ["reviewer"], "capabilities": ["review"],
            "driver": "codex_cli", "priority": 90,
        })
        protocol.save(af.project_dir("P"), data)
        af.cmd_goal("P", FakeArgs(action="start", text="Ship feature", owner="codex_cli"))
        plan = [
            {"title": "Build API", "role": "backend", "capabilities": ["python", "api"], "acceptance": ["API passes"]},
            {"title": "Audit release", "role": "reviewer", "capabilities": ["review"], "acceptance": ["release passes"], "final_acceptance": True},
        ]

        af.cmd_goal("P", FakeArgs(action="plan", context_summary="x", plan_json=json.dumps(plan)))

        state = af.load_state("P")
        self.assertEqual(state["task"]["owner"], "builder")
        self.assertEqual(state["goal"]["queue"][0]["owner"], "auditor")
        self.assertTrue(state["goal"]["queue"][0]["independent_acceptance"])

    def test_new_goal_is_queued_instead_of_replacing_active_goal(self):
        self._use_legacy_goal_workflow()
        af.cmd_goal("P", FakeArgs(action="start", text="First", owner="codex_cli", replace=False))
        first_id = af.load_state("P")["goal"]["id"]

        af.cmd_goal("P", FakeArgs(action="start", text="Second", owner="codex_cli", replace=False))

        state = af.load_state("P")
        self.assertEqual(state["goal"]["id"], first_id)
        self.assertEqual(state["goal_queue"][0]["text"], "Second")
        self.assertEqual(state["goal_queue"][0]["status"], "queued")

    def test_queued_goal_activates_after_current_goal_finishes(self):
        self._use_legacy_goal_workflow()
        af.cmd_goal("P", FakeArgs(action="start", text="First", owner="codex_cli", replace=False))
        first_plan = [{
            "title": "Review first", "owner": "codex_cli", "role": "reviewer",
            "acceptance": ["first passes"], "final_acceptance": True,
        }]
        af.cmd_goal("P", FakeArgs(action="plan", context_summary="first", plan_json=json.dumps(first_plan)))
        af.cmd_goal("P", FakeArgs(action="start", text="Second", owner="codex_cli", replace=False))

        af.cmd_task("P", FakeArgs(action="complete", output="First done", next=None, json=False))

        state = af.load_state("P")
        self.assertEqual(state["goal"]["text"], "Second")
        self.assertEqual(state["goal"]["status"], "planning")
        self.assertEqual(state["task"]["required_role"], "planner")
        self.assertEqual(state["goal_history"][0]["text"], "First")

    def test_final_acceptance_reroutes_away_from_implementation_owner(self):
        self._use_legacy_goal_workflow()
        data = af.load_protocol("P")
        data["agents"]["worker"] = protocol.normalize_agent("worker", {
            "roles": ["implementation", "reviewer"], "capabilities": ["*"],
            "driver": "cursor_cli", "priority": 90,
        })
        data["agents"]["auditor"] = protocol.normalize_agent("auditor", {
            "roles": ["reviewer"], "capabilities": ["*"],
            "driver": "codex_cli", "priority": 80,
        })
        protocol.save(af.project_dir("P"), data)
        af.cmd_goal("P", FakeArgs(action="start", text="Ship", owner="codex_cli"))
        plan = [
            {"title": "Build", "owner": "worker", "acceptance": ["built"]},
            {"title": "Review", "owner": "worker", "role": "reviewer", "acceptance": ["reviewed"], "final_acceptance": True},
        ]

        af.cmd_goal("P", FakeArgs(action="plan", context_summary="x", plan_json=json.dumps(plan)))

        final = af.load_state("P")["goal"]["queue"][0]
        self.assertEqual(final["owner"], "auditor")
        self.assertTrue(final["independent_acceptance"])

    def test_goal_plan_rejects_final_acceptance_without_independent_agent(self):
        self._use_legacy_goal_workflow()
        data = af.load_protocol("P")
        data["agents"] = {
            "worker": protocol.normalize_agent("worker", {
                "roles": ["planner", "implementation", "reviewer"],
                "capabilities": ["*"], "driver": "codex_cli",
            }),
        }
        protocol.save(af.project_dir("P"), data)
        af.cmd_goal("P", FakeArgs(action="start", text="Ship", owner="worker"))
        plan = [
            {"title": "Build", "owner": "worker", "acceptance": ["built"]},
            {"title": "Review", "owner": "worker", "role": "reviewer", "acceptance": ["reviewed"], "final_acceptance": True},
        ]

        with self.assertRaisesRegex(ValueError, "independent executable agent"):
            af.cmd_goal("P", FakeArgs(action="plan", context_summary="x", plan_json=json.dumps(plan)))

    def test_goal_plan_rejects_overly_fine_decomposition(self):
        self._use_legacy_goal_workflow()
        af.cmd_goal("P", FakeArgs(action="start", text="Ship feature", owner="codex_cli"))
        plan = [{"title": f"tiny {i}", "owner": "builder", "acceptance": ["done"], "final_acceptance": i == 7} for i in range(8)]
        with self.assertRaisesRegex(ValueError, "1 to 7"):
            af.cmd_goal("P", FakeArgs(action="plan", context_summary="x", plan_json=json.dumps(plan)))

    def test_goal_does_not_advance_when_current_milestone_fails_verification(self):
        self._use_legacy_goal_workflow()
        data = af.load_protocol("P")
        data["agents"]["builder"] = protocol.normalize_agent("builder", {
            "roles": ["implementation"], "driver": "cursor_cli",
        })
        protocol.save(af.project_dir("P"), data)
        af.cmd_goal("P", FakeArgs(action="start", text="Ship feature", owner="codex_cli"))
        plan = [
            {"title": "Build", "owner": "builder", "acceptance": ["passes"], "verify_commands": ["test"]},
            {"title": "Integrate", "owner": "codex_cli", "acceptance": ["passes"], "final_acceptance": True},
        ]
        af.cmd_goal("P", FakeArgs(action="plan", context_summary="x", plan_json=json.dumps(plan)))
        with patch("plow_whip.agent_flow.subprocess.run", return_value=Mock(returncode=1, stdout="", stderr="fail")):
            af.cmd_task("P", FakeArgs(action="complete", output="attempt", next=None, json=False))
        state = af.load_state("P")
        self.assertEqual(state["task"]["title"], "Build")
        self.assertEqual(state["goal"]["completed"], [])
        self.assertEqual(len(state["goal"]["queue"]), 1)

    def test_task_completion_archives_cli_sessions(self):
        state = af.load_state("P")
        state["task"]["active_session"] = {
            "agent": "codex_cli", "session_id": "codex-1", "status": "active",
        }
        state["task"]["blockers"] = ["old blocker"]
        af.save_state("P", state)
        af.cmd_task("P", FakeArgs(action="complete", output="Done", next=None, json=False))
        task = af.load_state("P")["task"]
        self.assertEqual(task["blockers"], [])
        self.assertIsNone(task["active_session"])
        self.assertEqual(task["session_archive"][0]["status"], "archived")
        archive = os.path.join(self.projects, "P", "collab", "memory", "sessions", "T-001_cli_sessions.json")
        self.assertTrue(os.path.exists(archive))
        with open(archive, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["sessions"][0]["session_id"], "codex-1")

    def test_scheduler_renders_three_native_formats(self):
        mac = scheduler.render(self.config, system="Darwin")
        linux = scheduler.render(self.config, system="Linux")
        windows = scheduler.render(self.config, system="Windows")
        self.assertIn("StartInterval", mac["content"])
        self.assertIn("<key>PATH</key>", mac["content"])
        self.assertIn("OnUnitActiveSec=60", linux["content"]["timer"])
        self.assertIn("Environment=PATH=", linux["content"]["service"])
        self.assertEqual(windows["content"]["task_name"], "PlowWhipScheduler")
        self.assertTrue(any("--once" in arg for arg in windows["content"]["arguments"]))
        self.assertNotIn("--auto-rotate", scheduler.command())
        self.assertIn("--crack", scheduler.command())
        self.assertIn("--opt-in-only", scheduler.command())
        continuing = scheduler.render(self.config, auto_crack=True, system="Linux")
        self.assertTrue(continuing["auto_continue"])
        self.assertIn("--crack", continuing["content"]["service"])
        self.assertIn("--opt-in-only", continuing["content"]["service"])
        self.assertIn("--stale-minutes 1", continuing["content"]["service"])
        escaped = scheduler.render(os.path.join(self.config, "a&b"), system="Darwin")
        self.assertIn("a&amp;b", escaped["content"])
        with self.assertRaises(ValueError):
            scheduler.render(self.config, interval=0)

    @patch("plow_whip.scheduler.shutil.which", return_value="/opt/tools/plow-whip")
    def test_scheduler_prefers_installed_absolute_entrypoint(self, _mock_which):
        self.assertEqual(scheduler.command()[0], "/opt/tools/plow-whip")

    def test_whip_once_releases_lock(self):
        first = run_once(auto_rotate=False)
        second = run_once(auto_rotate=False)
        self.assertEqual(first["status"], "ok")
        self.assertEqual(second["status"], "ok")

    def test_whip_once_returns_probe_fields_only(self):
        af.cmd_init("ProbeOnly")
        payload = run_once(auto_rotate=False)
        self.assertEqual(payload["mode"], "probe")
        self.assertFalse(payload["context_loaded"])
        self.assertFalse(payload["model_invoked"])
        project = next(item for item in payload["projects"] if item["project"] == "ProbeOnly")
        self.assertIn("task_id", project)
        self.assertIn("task_status", project)
        self.assertNotIn("task", project)
        self.assertNotIn("next_action", project)
        self.assertNotIn("task_context", project)

    def test_scheduled_recovery_only_includes_projects_that_opted_in(self):
        af.cmd_init("Other")
        for project, enabled in (("P", True), ("Other", False)):
            state = af.load_state(project)
            state["automation_enabled"] = enabled
            state["updated_at"] = ""
            af.write_state(project, state, touch=False)
        payload = run_once(stale_minutes=1, crack=False, opt_in_only=True)
        self.assertIn("P", payload["recovery_projects"])
        self.assertNotIn("Other", payload["recovery_projects"])

    @patch("plow_whip.whip._load_recovery_project")
    def test_whip_probe_never_hydrates_recovery_context(self, mock_hydrate):
        run_once(crack=False)
        mock_hydrate.assert_not_called()


if __name__ == "__main__":
    unittest.main()

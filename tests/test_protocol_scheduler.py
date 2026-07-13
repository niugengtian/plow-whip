"""Canonical protocol, startup payload, and native scheduler tests."""

import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import Mock, patch

import plow_whip.agent_flow as af
from plow_whip import protocol, routing, scheduler
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
        self.assertEqual(pack["task"]["id"], "T-001")
        self.assertIn("R001", [r["id"] for r in pack["mandatory_rules"]])
        self.assertNotIn("R006", [r["id"] for r in pack["important_rules"]])
        self.assertTrue(pack["rules_meta"]["effective_hash"])
        for rule in pack["mandatory_rules"] + pack["important_rules"]:
            self.assertIn(rule["scope"], ("global", "project"))
            self.assertIn(rule["priority"], ("required", "important"))
            self.assertEqual(rule["origin"], "derived")
            self.assertTrue(rule["derived_from"])
            self.assertIn(rule["enforcement"], ("block", "require_approval", "verify", "warn", "inform"))
        self.assertNotIn("next_action_file_excerpt", pack)
        self.assertNotIn("rules", pack)
        self.assertLessEqual(pack["rules_meta"]["startup_payload_chars"], af.START_PACK_MAX_CHARS)

    def test_start_pack_clamps_unbounded_task_fields(self):
        state = af.load_state("P")
        state["task"]["title"] = "T" * 10000
        state["task"]["goal"] = "G" * 10000
        state["task"]["next_action"] = "N" * 10000
        state["task"]["acceptance"] = ["A" * 5000 for _ in range(100)]
        af.save_state("P", state)

        pack = af.build_start_pack("P", "codex")

        self.assertTrue(pack["ready"])
        self.assertTrue(pack["task"]["title_truncated"])
        self.assertTrue(pack["task"]["next_action_truncated"])
        self.assertEqual(len(pack["task"]["acceptance"]), 10)
        self.assertLessEqual(pack["rules_meta"]["startup_payload_chars"], af.START_PACK_MAX_CHARS)

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
        self.assertNotIn("P-agent", [r["id"] for r in codex["important_rules"]])
        self.assertNotIn("P-agent", [r["id"] for r in builder_without_tag["important_rules"]])
        self.assertIn("P-agent", [r["id"] for r in builder["important_rules"]])
        self.assertEqual(builder["required_context"][0]["rule_id"], "P-agent")

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

        self.assertIn("P-late", [rule["id"] for rule in pack["important_rules"]])
        self.assertNotIn("late-tag", pack["task"]["rule_tags"])

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

    def test_goal_plan_advances_coarse_milestones_and_finishes_only_at_end(self):
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

    def test_goal_compatibility_path_uses_configured_default_planner(self):
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
        af.cmd_goal("P", FakeArgs(action="start", text="First", owner="codex_cli", replace=False))
        first_id = af.load_state("P")["goal"]["id"]

        af.cmd_goal("P", FakeArgs(action="start", text="Second", owner="codex_cli", replace=False))

        state = af.load_state("P")
        self.assertEqual(state["goal"]["id"], first_id)
        self.assertEqual(state["goal_queue"][0]["text"], "Second")
        self.assertEqual(state["goal_queue"][0]["status"], "queued")

    def test_queued_goal_activates_after_current_goal_finishes(self):
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
        af.cmd_goal("P", FakeArgs(action="start", text="Ship feature", owner="codex_cli"))
        plan = [{"title": f"tiny {i}", "owner": "builder", "acceptance": ["done"], "final_acceptance": i == 7} for i in range(8)]
        with self.assertRaisesRegex(ValueError, "1 to 7"):
            af.cmd_goal("P", FakeArgs(action="plan", context_summary="x", plan_json=json.dumps(plan)))

    def test_goal_does_not_advance_when_current_milestone_fails_verification(self):
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
        state["task"]["cli_sessions"] = {
            "cursor_cli": {"session_id": "cursor-1", "status": "active"},
            "codex_cli": {"session_id": "codex-1", "status": "active"},
        }
        state["task"]["blockers"] = ["old blocker"]
        af.save_state("P", state)
        af.cmd_task("P", FakeArgs(action="complete", output="Done", next=None, json=False))
        task = af.load_state("P")["task"]
        self.assertEqual(task["blockers"], [])
        self.assertTrue(all(item["status"] == "archived" for item in task["cli_sessions"].values()))
        archive = os.path.join(self.projects, "P", "collab", "memory", "sessions", "T-001_cli_sessions.json")
        self.assertTrue(os.path.exists(archive))
        with open(archive, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["cli_sessions"]["cursor_cli"]["session_id"], "cursor-1")

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

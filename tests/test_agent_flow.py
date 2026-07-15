"""Tests for plow-whip agent_flow engine."""

import json
import copy
import os
import shutil
import tempfile
import unittest

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import plow_whip.agent_flow as af
from plow_whip import leases, protocol
from plow_whip.agent_flow import (
    cmd_agent,
    cmd_configure,
    cmd_new,
    build_start_pack,
    build_doctor_report,
    build_memory_budget,
    cmd_doctor,
    format_memory_budget,
    cmd_handoff,
    cmd_init,
    cmd_list,
    cmd_rotate,
    cmd_session,
    cmd_sessions_overview,
    cmd_status,
    cmd_sync,
    conventions_agent_file,
    conventions_human_file,
    load_config,
    load_state,
    save_config,
)


class PlowWhipTestBase(unittest.TestCase):
    """Base class that sets up a temp environment for each test."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.projects_dir = os.path.join(self.tmpdir, "projects")
        os.makedirs(self.projects_dir)
        self.config_dir = os.path.join(self.tmpdir, "config")
        os.makedirs(self.config_dir)
        self.config_file = os.path.join(self.config_dir, "config.json")

        # Patch config paths
        self._orig_config_file = af.CONFIG_FILE
        self._orig_config_dir = af.CONFIG_DIR
        af.CONFIG_FILE = self.config_file
        af.CONFIG_DIR = self.config_dir

        # Write initial config
        save_config({
            "projects_dir": self.projects_dir,
            "agents": ["qoder", "codex", "cursor"],
            "agent_meta": {
                "cursor": {
                    "roles": ["implementation"], "capabilities": ["*"],
                    "driver": "zellij", "schedulable": True,
                },
            },
        })

    def tearDown(self):
        af.CONFIG_FILE = self._orig_config_file
        af.CONFIG_DIR = self._orig_config_dir
        shutil.rmtree(self.tmpdir)


class TestConfigure(PlowWhipTestBase):
    def test_project_name_cannot_escape_projects_root(self):
        with self.assertRaises(ValueError):
            af.project_dir("../escape")

    def test_configure_creates_config(self):
        os.remove(self.config_file)
        args = FakeArgs(projects_dir=self.projects_dir, agents=["qoder", "codex"])
        cmd_configure(args)
        cfg = load_config()
        self.assertEqual(cfg["projects_dir"], self.projects_dir)
        self.assertEqual(cfg["agents"], ["qoder", "codex"])


class TestInit(PlowWhipTestBase):
    def test_collab_template_is_framework_owned_and_not_copied_to_project(self):
        self.assertEqual(os.path.basename(af.template_dir()), "collab.template")
        self.assertTrue(os.path.exists(os.path.join(af.template_dir(), "AGENT_COMMS.md.tpl")))
        self.assertTrue(os.path.exists(os.path.join(af.template_dir(), "conversations", "current.md.tpl")))
        cmd_init("TestProject")
        project_root = os.path.join(self.projects_dir, "TestProject")
        self.assertFalse(os.path.exists(os.path.join(project_root, "collab.template")))
        self.assertTrue(os.path.exists(os.path.join(project_root, "collab", "AGENT_COMMS.md")))

    def test_init_creates_collab_structure(self):
        cmd_init("TestProject")
        collab_dir = os.path.join(self.projects_dir, "TestProject", "collab")
        self.assertTrue(os.path.isdir(collab_dir))
        self.assertTrue(os.path.exists(os.path.join(collab_dir, "AGENT_STATE.json")))
        self.assertTrue(os.path.exists(os.path.join(collab_dir, "AGENT_COMMS.md")))
        self.assertTrue(os.path.exists(os.path.join(collab_dir, "AGENTS.md")))
        self.assertTrue(os.path.exists(os.path.join(collab_dir, "CONVENTIONS.md")))
        self.assertTrue(os.path.exists(os.path.join(collab_dir, "CONVENTIONS.agent.md")))
        for agent in ("qoder", "codex", "cursor"):
            self.assertTrue(os.path.exists(os.path.join(collab_dir, "conversations", agent, "current.md")))
        for filename in ("DECISIONS.md",):
            self.assertTrue(os.path.exists(os.path.join(collab_dir, "memory", filename)))
        self.assertTrue(os.path.isdir(os.path.join(collab_dir, "memory", "sessions")))


    def test_init_writes_subagent_boundary_rule(self):
        cmd_init("TestProject")
        agent_path = os.path.join(self.projects_dir, "TestProject", "collab", "AGENT_PROTOCOL.json")
        human_path = os.path.join(self.projects_dir, "TestProject", "collab", "HANDBOOK.zh-CN.md")
        with open(agent_path, encoding="utf-8") as f:
            agent = json.load(f)
        with open(human_path, encoding="utf-8") as f:
            human = f.read()
        self.assertIn("R004", agent["global_rules"])
        self.assertIn("子智能体", human)

    def test_init_writes_project_boundary_rule(self):
        cmd_init("TestProject")
        human_path = os.path.join(self.projects_dir, "TestProject", "collab", "HANDBOOK.zh-CN.md")
        with open(human_path, encoding="utf-8") as f:
            conventions = f.read()
        self.assertIn("项目根目录", conventions)

    def test_init_creates_valid_state(self):
        cmd_init("TestProject")
        state = load_state("TestProject")
        self.assertEqual(state["current_agent"], "qoder")
        self.assertNotIn("project_path", state["task_context"])
        self.assertEqual(af.get_project_agents("TestProject"), ["qoder", "codex", "cursor"])

    def test_stale_state_writer_cannot_overwrite_newer_agent_work(self):
        cmd_init("TestProject")
        first = load_state("TestProject")
        stale = copy.deepcopy(first)
        first["task"]["next_action"] = "newer work"
        af.save_state("TestProject", first)
        stale["task"]["next_action"] = "stale overwrite"
        with self.assertRaises(RuntimeError):
            af.save_state("TestProject", stale)
        self.assertEqual(load_state("TestProject")["task"]["next_action"], "newer work")

    def test_init_writes_agent_manifest_from_config(self):
        save_config({
            "projects_dir": self.projects_dir,
            "agents": ["planner", "builder"],
            "agent_meta": {
                "planner": {"role": "PM", "assignment": "Plan work"},
                "builder": {"role": "Engineer", "assignment": "Build work"},
            },
        })
        cmd_init("TestProject")
        state = load_state("TestProject")
        self.assertEqual(state["current_agent"], "planner")
        self.assertEqual(af.load_protocol("TestProject")["agents"]["builder"]["assignment"], "Build work")
        with open(os.path.join(self.projects_dir, "TestProject", "collab", "AGENTS.md"), encoding="utf-8") as f:
            manifest = f.read()
        self.assertIn("| `planner` | pm | file | yes | — | Plan work |", manifest)
        self.assertIn("| `builder` | engineer | file | yes | — | Build work |", manifest)

    def test_agent_set_updates_config_and_project(self):
        cmd_init("TestProject")
        args = FakeArgs(action="set", name="reviewer", role="Reviewer", assignment="Review work")
        cmd_agent(args, project="TestProject")
        cfg = load_config()
        self.assertNotIn("reviewer", cfg["agents"])
        protocol = af.load_protocol("TestProject")
        self.assertIn("reviewer", protocol["agents"])
        self.assertEqual(protocol["agents"]["reviewer"]["role"], "Reviewer")
        current = os.path.join(self.projects_dir, "TestProject", "collab", "conversations", "reviewer", "current.md")
        self.assertTrue(os.path.exists(current))
        with open(current, encoding="utf-8") as f:
            self.assertIn("**AI:** Reviewer", f.read())

    def test_init_idempotent(self):
        cmd_init("TestProject")
        cmd_init("TestProject")  # Should not error

    def test_new_creates_project_with_first_action_and_owner(self):
        args = FakeArgs(first_action="Draft the project plan", owner="cursor")
        cmd_new("NewProject", args)
        state = load_state("NewProject")
        self.assertEqual(state["task"]["owner"], "cursor")
        self.assertEqual(state["next_action"], "Draft the project plan")
        self.assertTrue(os.path.exists(os.path.join(self.projects_dir, "NewProject", "collab", "CONVENTIONS.md")))


class TestContextPack(PlowWhipTestBase):
    def test_context_pack_alias_uses_start_payload(self):
        cmd_init("TestProject")
        af.append_comms("TestProject", "@cursor please ignore this")
        af.append_comms("TestProject", "@codex please implement the tiny context pack")
        pack = build_start_pack("TestProject", agent="codex")
        self.assertEqual(pack["agent"], "codex")
        self.assertIn("@codex please implement", "\n".join(pack["messages"]))
        self.assertNotIn("@cursor please ignore", "\n".join(pack["messages"]))
        self.assertNotIn("read_first", pack)


class TestDoctor(PlowWhipTestBase):
    def test_doctor_reports_missing_before_repair(self):
        report = build_doctor_report("NeedsPlowWhip")
        self.assertFalse(report["ok"])
        self.assertTrue(report["missing"])

    def test_doctor_repair_creates_plow_whip_structure(self):
        args = FakeArgs(repair=True, json=False)
        cmd_doctor("NeedsPlowWhip", args)
        report = build_doctor_report("NeedsPlowWhip")
        self.assertTrue(report["ok"])
        self.assertTrue(os.path.exists(os.path.join(self.projects_dir, "NeedsPlowWhip", "collab", "AGENT_STATE.json")))
        self.assertTrue(os.path.exists(os.path.join(self.projects_dir, "NeedsPlowWhip", "collab", "CONVENTIONS.md")))
        self.assertTrue(os.path.exists(os.path.join(self.projects_dir, "NeedsPlowWhip", "collab", "CONVENTIONS.agent.md")))

    def test_doctor_repair_enforces_rotation_by_default(self):
        cmd_init("TestProject")
        with open(af.comms_file("TestProject"), "w", encoding="utf-8") as f:
            f.write("# board\n\n")
            for i in range(5):
                body = "\n".join(f"line {n}" for n in range(20))
                f.write(f"### [codex] 2026-07-12 — Msg {i}\n\n{body}\n\n")

        cmd_doctor("TestProject", FakeArgs(repair=True, skip_rotate=False, json=True))

        self.assertFalse(af.build_rotation_health("TestProject")["needs_enforcement"])

    def test_doctor_quarantines_unsigned_state_tampering(self):
        cmd_init("TestProject")
        with open(af.state_file("TestProject"), encoding="utf-8") as f:
            unsigned = json.load(f)
        data = af.load_protocol("TestProject")
        data["enforcement"]["reserved_hardening"]["state_hmac"] = True
        protocol.save(af.project_dir("TestProject"), data)
        leases.sign_state(af.CONFIG_DIR, "TestProject", unsigned, data)
        with open(af.state_file("TestProject"), "w", encoding="utf-8") as f:
            json.dump(unsigned, f)
        with open(af.state_file("TestProject"), encoding="utf-8") as f:
            state = json.load(f)
        state["next_action"] = "wrong duplicate"
        with open(af.state_file("TestProject"), "w", encoding="utf-8") as f:
            json.dump(state, f)
        report = build_doctor_report("TestProject")
        self.assertFalse(report["ok"])
        self.assertIn("integrity check failed", report["issues"][0])
        af.cmd_repair("TestProject", FakeArgs(json=True))
        self.assertFalse(build_doctor_report("TestProject")["ok"])

    def test_doctor_reports_corrupt_protocol_without_crashing(self):
        cmd_init("TestProject")
        with open(af.protocol_file("TestProject"), "w", encoding="utf-8") as f:
            f.write("{")
        report = build_doctor_report("TestProject")
        self.assertFalse(report["ok"])
        self.assertIn("invalid AGENT_PROTOCOL.json", report["issues"][0])
        af.cmd_repair("TestProject", FakeArgs(json=True))
        pack = af.build_start_pack("TestProject", "qoder")
        self.assertEqual(pack["action_required"], "fix_canonical_json")

    def test_doctor_detects_and_repairs_protocol_and_handbook_drift(self):
        cmd_init("TestProject")
        data = af.load_protocol("TestProject")
        data["schema_version"] = 3
        data["global_rules"]["R005"]["summary"] = "stale wording"
        data["orchestration"].pop("retry_limit")
        af.proto.save(af.project_dir("TestProject"), data)
        with open(af.handbook_file("TestProject"), "a", encoding="utf-8") as f:
            f.write("\nmanual drift\n")

        report = build_doctor_report("TestProject")

        self.assertFalse(report["ok"])
        self.assertTrue(any(issue.startswith("protocol schema drifted") for issue in report["issues"]))
        self.assertTrue(any(issue.startswith("inherited global rules drifted") for issue in report["issues"]))
        self.assertTrue(any(issue.startswith("protocol defaults missing") for issue in report["issues"]))
        self.assertTrue(any(issue.startswith("derived handbook drifted") for issue in report["issues"]))
        af.cmd_repair("TestProject", FakeArgs(json=True))
        self.assertTrue(build_doctor_report("TestProject")["ok"])


class TestStatus(PlowWhipTestBase):
    def test_status_runs(self):
        cmd_init("TestProject")
        cmd_status("TestProject")


class TestHandoff(PlowWhipTestBase):
    def test_handoff_switches_agent(self):
        cmd_init("TestProject")
        self.assertEqual(load_state("TestProject")["current_agent"], "qoder")

        args = FakeArgs(
            output="Done A", next="Do B", phase="Sprint-1",
            status="done", day=None, topic=None, project_dir=None,
            files=None, verify=None, to=None, blockers=None,
        )
        cmd_handoff("TestProject", args)
        state = load_state("TestProject")
        self.assertEqual(state["current_agent"], "cursor")
        self.assertEqual(state["last_output"], "Done A")

    def test_handoff_can_assign_agent_and_record_blockers(self):
        cmd_init("TestProject")
        args = FakeArgs(
            output="Done A", next="Review B", phase="Sprint-1",
            status="in_progress", day=None, topic=None, project_dir=None,
            files=None, verify=None, to="cursor", blockers=["needs-review"],
        )
        cmd_handoff("TestProject", args)
        state = load_state("TestProject")
        self.assertEqual(state["current_agent"], "cursor")
        self.assertEqual(state["assigned_agent"], "cursor")
        self.assertEqual(state["blockers"], ["needs-review"])

    def test_handoff_round_robin(self):
        cmd_init("TestProject")
        args = FakeArgs(output="A", next="B", phase="P", status="done",
                        day=None, topic=None, project_dir=None, files=None, verify=None, to=None, blockers=None)
        cmd_handoff("TestProject", args)
        self.assertEqual(load_state("TestProject")["current_agent"], "cursor")
        cmd_handoff("TestProject", args)
        self.assertEqual(load_state("TestProject")["current_agent"], "qoder")


class TestSession(PlowWhipTestBase):
    def test_session_runs(self):
        cmd_init("TestProject")
        cmd_session("TestProject", "qoder")

    def test_sessions_overview_runs(self):
        cmd_init("TestProject")
        cmd_sessions_overview("TestProject")


class TestRotate(PlowWhipTestBase):
    def test_rotate_creates_archive(self):
        cmd_init("TestProject")
        conv_dir = os.path.join(self.projects_dir, "TestProject", "collab", "conversations", "qoder")
        curr = os.path.join(conv_dir, "current.md")

        with open(curr, "w") as f:
            f.write("# Test content\n- done: implemented context pack\n- next: add budget check\n" * 50)

        args = FakeArgs(topic="Test Session", summary="Test summary", agent="qoder")
        cmd_rotate("TestProject", "qoder", args)

        archives = [f for f in os.listdir(conv_dir) if f != "current.md"]
        self.assertEqual(len(archives), 1)
        with open(os.path.join(conv_dir, archives[0]), encoding="utf-8") as f:
            archive_text = f.read()
        self.assertIn("## Carry Forward", archive_text)
        self.assertIn("Human summary: Test summary", archive_text)
        self.assertIn("add budget check", archive_text)
        self.assertTrue(os.path.exists(curr))


class TestMemoryBudget(PlowWhipTestBase):
    def test_memory_budget_reports_hot_warm_layers(self):
        cmd_init("TestProject")
        report = build_memory_budget("TestProject")
        rendered = format_memory_budget(report)
        self.assertIn("hot", report["budgets"])
        self.assertIn("Hot:", rendered)
        self.assertIn("Warm:", rendered)
        self.assertIn("AGENT_STATE.json", rendered)
        self.assertIn("Machine truth", rendered)
        self.assertNotIn("AGENT_PROTOCOL.json", [item["file"] for item in report["hot"]["files"]])
        self.assertTrue(report["hot"]["tokens"] > 0)


class TestSync(PlowWhipTestBase):
    def test_sync_runs(self):
        cmd_init("TestProject")
        cmd_sync()

    def test_sync_recreates_conventions(self):
        cmd_init("TestProject")
        agent_path = conventions_agent_file("TestProject")
        human_path = conventions_human_file("TestProject")
        os.remove(agent_path)
        os.remove(human_path)
        self.assertFalse(os.path.exists(agent_path))
        cmd_sync()
        self.assertTrue(os.path.exists(agent_path))
        self.assertTrue(os.path.exists(human_path))

    def test_agent_change_syncs_to_human(self):
        cmd_init("TestProject")
        data = af.load_protocol("TestProject")
        data["project_rules"]["R101"] = {"summary": "Probe rule.", "summary_zh": "同步探测规则。"}
        af.proto.save(af.project_dir("TestProject"), data)
        af.proto.write_handbook(af.project_dir("TestProject"), data)
        human_path = af.handbook_file("TestProject")
        with open(human_path, encoding="utf-8") as f:
            human = f.read()
        self.assertIn("同步探测规则", human)



    def test_sync_preserves_project_principles(self):
        cmd_init("TestProject")
        data = af.load_protocol("TestProject")
        data["project_rules"]["R101"] = {"summary": "Keep this project-only rule."}
        af.proto.save(af.project_dir("TestProject"), data)
        cmd_sync()
        with open(af.handbook_file("TestProject"), encoding="utf-8") as f:
            conventions = f.read()
        self.assertIn("Keep this project-only rule", conventions)

    def test_sync_migrates_legacy_conventions_without_overwriting(self):
        cmd_init("TestProject")
        human_path = conventions_human_file("TestProject")
        agent_path = conventions_agent_file("TestProject")
        os.remove(agent_path)
        with open(human_path, "w", encoding="utf-8") as f:
            f.write("# Legacy conventions\n\n- Legacy project rule.\n")
        cmd_sync()
        self.assertTrue(os.path.exists(agent_path))
        with open(human_path, encoding="utf-8") as f:
            conventions = f.read()
        self.assertIn("HANDBOOK.zh-CN.md", conventions)
        self.assertTrue(os.path.exists(af.protocol_file("TestProject")))


class TestList(PlowWhipTestBase):
    def test_list_shows_projects(self):
        cmd_init("ProjectA")
        cmd_init("ProjectB")
        cmd_list()


class FakeArgs:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


if __name__ == "__main__":
    unittest.main()

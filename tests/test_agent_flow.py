"""Tests for plow-whip agent_flow engine."""

import json
import os
import shutil
import tempfile
import unittest

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import plow_whip.agent_flow as af
from plow_whip.agent_flow import (
    cmd_agent,
    cmd_configure,
    cmd_new,
    build_context_pack,
    build_doctor_report,
    build_memory_budget,
    cmd_doctor,
    format_memory_budget,
    format_context_pack,
    cmd_handoff,
    cmd_init,
    cmd_list,
    cmd_rotate,
    cmd_session,
    cmd_sessions_overview,
    cmd_status,
    cmd_sync,
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
        })

    def tearDown(self):
        af.CONFIG_FILE = self._orig_config_file
        af.CONFIG_DIR = self._orig_config_dir
        shutil.rmtree(self.tmpdir)


class TestConfigure(PlowWhipTestBase):
    def test_configure_creates_config(self):
        os.remove(self.config_file)
        args = FakeArgs(projects_dir=self.projects_dir, agents=["qoder", "codex"])
        cmd_configure(args)
        cfg = load_config()
        self.assertEqual(cfg["projects_dir"], self.projects_dir)
        self.assertEqual(cfg["agents"], ["qoder", "codex"])


class TestInit(PlowWhipTestBase):
    def test_init_creates_collab_structure(self):
        cmd_init("TestProject")
        collab_dir = os.path.join(self.projects_dir, "TestProject", "collab")
        self.assertTrue(os.path.isdir(collab_dir))
        self.assertTrue(os.path.exists(os.path.join(collab_dir, "AGENT_STATE.json")))
        self.assertTrue(os.path.exists(os.path.join(collab_dir, "AGENT_COMMS.md")))
        self.assertTrue(os.path.exists(os.path.join(collab_dir, "AGENTS.md")))
        self.assertTrue(os.path.exists(os.path.join(collab_dir, "CONVENTIONS.md")))
        for agent in ("qoder", "codex", "cursor"):
            self.assertTrue(os.path.exists(os.path.join(collab_dir, "conversations", agent, "current.md")))
        for filename in ("PROJECT.md", "CURRENT_STATUS.md", "NEXT_ACTION.md", "ROADMAP.md", "DECISIONS.md"):
            self.assertTrue(os.path.exists(os.path.join(collab_dir, "memory", filename)))
        self.assertTrue(os.path.isdir(os.path.join(collab_dir, "memory", "adr")))
        self.assertTrue(os.path.isdir(os.path.join(collab_dir, "memory", "sessions")))
        self.assertTrue(os.path.isdir(os.path.join(collab_dir, "memory", "sprints", "active")))
        self.assertTrue(os.path.isdir(os.path.join(collab_dir, "memory", "sprints", "archive")))


    def test_init_writes_subagent_boundary_rule(self):
        cmd_init("TestProject")
        conventions_path = os.path.join(self.projects_dir, "TestProject", "collab", "CONVENTIONS.md")
        with open(conventions_path, encoding="utf-8") as f:
            conventions = f.read()
        self.assertIn("子智能体边界", conventions)
        self.assertIn("不允许自行创建、调用、委派或并行启动子智能体", conventions)
        self.assertIn("D-009", conventions)


    def test_init_writes_project_boundary_rule(self):
        cmd_init("TestProject")
        conventions_path = os.path.join(self.projects_dir, "TestProject", "collab", "CONVENTIONS.md")
        with open(conventions_path, encoding="utf-8") as f:
            conventions = f.read()
        self.assertIn("项目边界原则", conventions)
        self.assertIn("只能读取和修改当前项目根目录内的文件", conventions)
        self.assertIn("D-010", conventions)

    def test_init_creates_valid_state(self):
        cmd_init("TestProject")
        state = load_state("TestProject")
        self.assertEqual(state["current_agent"], "qoder")
        self.assertEqual(state["task_context"]["project_path"], os.path.join(self.projects_dir, "TestProject"))
        self.assertEqual(state["agents"], ["qoder", "codex", "cursor"])

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
        self.assertEqual(state["agent_meta"]["builder"]["assignment"], "Build work")
        with open(os.path.join(self.projects_dir, "TestProject", "collab", "AGENTS.md"), encoding="utf-8") as f:
            manifest = f.read()
        self.assertIn("| `planner` | PM | Plan work |", manifest)
        self.assertIn("| `builder` | Engineer | Build work |", manifest)

    def test_agent_set_updates_config_and_project(self):
        cmd_init("TestProject")
        args = FakeArgs(action="set", name="reviewer", role="Reviewer", assignment="Review work")
        cmd_agent(args, project="TestProject")
        cfg = load_config()
        self.assertIn("reviewer", cfg["agents"])
        self.assertEqual(cfg["agent_meta"]["reviewer"]["assignment"], "Review work")
        state = load_state("TestProject")
        self.assertIn("reviewer", state["agents"])
        self.assertEqual(state["agent_meta"]["reviewer"]["role"], "Reviewer")

    def test_init_idempotent(self):
        cmd_init("TestProject")
        cmd_init("TestProject")  # Should not error

    def test_new_creates_project_with_first_action_and_owner(self):
        args = FakeArgs(first_action="Draft the project plan", owner="codex")
        cmd_new("NewProject", args)
        state = load_state("NewProject")
        self.assertEqual(state["current_agent"], "codex")
        self.assertEqual(state["next_action"], "Draft the project plan")
        self.assertTrue(os.path.exists(os.path.join(self.projects_dir, "NewProject", "collab", "CONVENTIONS.md")))


class TestContextPack(PlowWhipTestBase):
    def test_context_pack_contains_minimal_state_and_targeted_messages(self):
        cmd_init("TestProject")
        af.append_comms("TestProject", "@cursor please ignore this")
        af.append_comms("TestProject", "@codex please implement the tiny context pack")
        pack = build_context_pack("TestProject", agent="codex")
        rendered = format_context_pack(pack)
        self.assertEqual(pack["agent"], "codex")
        self.assertIn("qoder starts requirements analysis", rendered)
        self.assertIn("@codex please implement", rendered)
        self.assertNotIn("@cursor please ignore", rendered)
        self.assertIn("CONVENTIONS.md", rendered)


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
        self.assertEqual(state["current_agent"], "codex")
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
        self.assertEqual(load_state("TestProject")["current_agent"], "codex")
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
        self.assertTrue(report["hot"]["tokens"] > 0)


class TestSync(PlowWhipTestBase):
    def test_sync_runs(self):
        cmd_init("TestProject")
        cmd_sync()

    def test_sync_recreates_conventions(self):
        cmd_init("TestProject")
        conventions_path = os.path.join(self.projects_dir, "TestProject", "collab", "CONVENTIONS.md")
        os.remove(conventions_path)
        self.assertFalse(os.path.exists(conventions_path))
        cmd_sync()
        self.assertTrue(os.path.exists(conventions_path))


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

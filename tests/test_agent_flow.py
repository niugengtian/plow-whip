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
    cmd_configure,
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
        self.assertTrue(os.path.isdir(os.path.join(collab_dir, "conversations", "qoder")))
        self.assertTrue(os.path.isdir(os.path.join(collab_dir, "memory")))

    def test_init_creates_valid_state(self):
        cmd_init("TestProject")
        state = load_state("TestProject")
        self.assertEqual(state["current_agent"], "qoder")

    def test_init_idempotent(self):
        cmd_init("TestProject")
        cmd_init("TestProject")  # Should not error


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
            files=None, verify=None,
        )
        cmd_handoff("TestProject", args)
        state = load_state("TestProject")
        self.assertEqual(state["current_agent"], "codex")
        self.assertEqual(state["last_output"], "Done A")

    def test_handoff_round_robin(self):
        cmd_init("TestProject")
        args = FakeArgs(output="A", next="B", phase="P", status="done",
                        day=None, topic=None, project_dir=None, files=None, verify=None)
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
            f.write("# Test content\n" * 50)

        args = FakeArgs(topic="Test Session", summary="Test summary", agent="qoder")
        cmd_rotate("TestProject", "qoder", args)

        archives = [f for f in os.listdir(conv_dir) if f != "current.md"]
        self.assertEqual(len(archives), 1)
        self.assertTrue(os.path.exists(curr))


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

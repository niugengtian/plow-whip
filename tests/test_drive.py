"""Tests for plow-whip drive module — Desktop 驱使 CLI."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import plow_whip.agent_flow as af
from plow_whip.agent_flow import save_config
from plow_whip.drive import build_drive_prompt, cmd_drive_status


class DriveTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.projects_dir = os.path.join(self.tmpdir, "projects")
        os.makedirs(self.projects_dir)
        self.config_dir = os.path.join(self.tmpdir, "config")
        os.makedirs(self.config_dir)
        self.config_file = os.path.join(self.config_dir, "config.json")
        self._orig_config_file = af.CONFIG_FILE
        self._orig_config_dir = af.CONFIG_DIR
        af.CONFIG_FILE = self.config_file
        af.CONFIG_DIR = self.config_dir
        save_config({
            "projects_dir": self.projects_dir,
            "agents": ["codex", "cursor", "cursor_cli", "codex_cli"],
        })
        self.project = "TestProj"
        pdir = os.path.join(self.projects_dir, self.project)
        os.makedirs(os.path.join(pdir, "collab", "memory"), exist_ok=True)
        af.save_state(self.project, af.default_state(self.project))

    def tearDown(self):
        af.CONFIG_FILE = self._orig_config_file
        af.CONFIG_DIR = self._orig_config_dir

    def test_build_drive_prompt_contains_paths_and_task(self):
        prompt = build_drive_prompt(self.project, "cursor_cli", "实现登录 API", requested_by="codex")
        self.assertIn("cursor_cli", prompt)
        self.assertIn("codex", prompt)
        self.assertIn("实现登录 API", prompt)
        self.assertIn("context-pack --agent cursor_cli", prompt)
        self.assertIn("CONVENTIONS.md", prompt)
        self.assertIn("P0 by_rm", prompt)
        self.assertIn("inbox/cursor_cli.json", prompt)

    def test_drive_status_runs_without_error(self):
        log_dir = os.path.join(self.tmpdir, "logs")
        os.makedirs(log_dir, exist_ok=True)
        with open(os.path.join(log_dir, "cursor_cli_test.log"), "w") as f:
            f.write("line1\nline2\nDONE\n")
        # 不应抛异常
        cmd_drive_status(self.project, "cursor_cli", log_dir)


if __name__ == "__main__":
    unittest.main()

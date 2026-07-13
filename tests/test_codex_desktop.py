import json
import os
import shutil
import tempfile
import unittest
import io
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import plow_whip.agent_flow as af
from plow_whip import codex_desktop, protocol
from plow_whip.dispatch import dispatch


class CodexDesktopTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.projects = os.path.join(self.tmp, "projects")
        self.config = os.path.join(self.tmp, "config")
        self.codex_home = Path(self.tmp) / "codex-home"
        os.makedirs(self.projects)
        os.makedirs(self.config)
        self.old_file, self.old_dir = af.CONFIG_FILE, af.CONFIG_DIR
        af.CONFIG_DIR = self.config
        af.CONFIG_FILE = os.path.join(self.config, "config.json")
        af.save_config({"projects_dir": self.projects, "agents": ["codex", "codex_cli"]})
        af.cmd_init("P")
        self.thread_id = "thread-123"
        self.thread_file = self.codex_home / "sessions" / "2026" / "07" / "14" / f"rollout-now-{self.thread_id}.jsonl"
        self.thread_file.parent.mkdir(parents=True)

    def tearDown(self):
        af.CONFIG_FILE, af.CONFIG_DIR = self.old_file, self.old_dir
        shutil.rmtree(self.tmp)

    def _append(self, payload):
        with self.thread_file.open("a", encoding="utf-8") as file:
            file.write(json.dumps({"timestamp": "2026-07-14T00:00:00Z", "type": "response_item", "payload": payload}) + "\n")

    def test_sync_is_incremental_and_persists_only_allowed_text(self):
        self._append({"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "secret developer"}]})
        self._append({"type": "reasoning", "summary": [{"text": "secret reasoning"}]})
        self._append({"type": "message", "role": "user", "content": [{"type": "input_text", "text": "user request"}]})
        self._append({"type": "message", "role": "assistant", "phase": "commentary", "content": [{"type": "output_text", "text": "working"}]})
        self._append({"type": "message", "role": "assistant", "phase": "final", "content": [{"type": "output_text", "text": "done"}]})
        self._append({"type": "message", "role": "assistant", "phase": "analysis", "content": [{"type": "output_text", "text": "secret analysis"}]})
        env = {"CODEX_HOME": str(self.codex_home), "CODEX_THREAD_ID": self.thread_id}
        with patch.dict(os.environ, env):
            first = codex_desktop.sync("P")
            second = codex_desktop.sync("P")
        text = Path(af.conversations_dir("P"), "codex", "current.md").read_text()
        self.assertEqual(first["synced_messages"], 3)
        self.assertEqual(second["synced_messages"], 0)
        self.assertIn("user request", text)
        self.assertIn("assistant/commentary", text)
        self.assertIn("assistant/final", text)
        for excluded in ("secret developer", "secret reasoning", "secret analysis"):
            self.assertNotIn(excluded, text)

    def test_scheduler_can_continue_from_checkpoint_without_environment_thread(self):
        self._append({"type": "message", "role": "user", "content": [{"type": "input_text", "text": "one"}]})
        with patch.dict(os.environ, {"CODEX_HOME": str(self.codex_home), "CODEX_THREAD_ID": self.thread_id}):
            codex_desktop.sync("P")
        self._append({"type": "message", "role": "assistant", "phase": "final", "content": [{"type": "output_text", "text": "two"}]})
        with patch.dict(os.environ, {"CODEX_HOME": str(self.codex_home)}, clear=False):
            os.environ.pop("CODEX_THREAD_ID", None)
            result = codex_desktop.sync("P", allow_env=False)
        self.assertEqual(result["synced_messages"], 1)

    def test_control_plane_cannot_dispatch(self):
        result = dispatch("codex", "P", "work")
        self.assertEqual(result["status"], "rejected_control_plane")
        self.assertNotIn("execution", af.load_state("P")["task"])

    def test_control_plane_cannot_start_task(self):
        class Args:
            action = "start"
            task_id = "T-control"
            title = "work"
            goal = None
            owner = "codex"
            next = None
            acceptance = []
            verify = []
            rule_tags = []
            decisions = []

        with self.assertRaisesRegex(SystemExit, "2"):
            af.cmd_task("P", Args())

    def test_normalized_codex_is_control_plane_and_cli_is_independent(self):
        data = af.load_protocol("P")
        self.assertFalse(data["agents"]["codex"]["schedulable"])
        self.assertEqual(data["agents"]["codex"]["driver"], "control")
        self.assertTrue(data["agents"]["codex_cli"]["schedulable"])
        self.assertIn("planner", data["agents"]["codex_cli"]["roles"])

    def test_submit_records_desktop_interaction_and_assigns_cli(self):
        self._append({"type": "message", "role": "user", "content": [{"type": "input_text", "text": "run tests with codex cli"}]})

        class Args:
            text = "Use codex cli to run tests"
            cli = "codex_cli"
            planner = None
            target_branch = None
            source = "current_session"
            replace = False

        with patch.dict(os.environ, {"CODEX_HOME": str(self.codex_home), "CODEX_THREAD_ID": self.thread_id}):
            with redirect_stdout(io.StringIO()):
                af.cmd_submit("P", Args())
        state = af.load_state("P")
        self.assertEqual(state["task"]["owner"], "codex_cli")
        self.assertEqual(state["workflow"]["interaction"]["thread_id"], self.thread_id)


if __name__ == "__main__":
    unittest.main()

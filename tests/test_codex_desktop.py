import json
import os
import shutil
import tempfile
import unittest
import io
import hashlib
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
        self.thread_ref = f"sha256:{hashlib.sha256(self.thread_id.encode('utf-8')).hexdigest()}"
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
        self._append({"type": "message", "role": "assistant", "phase": "final_answer", "content": [{"type": "output_text", "text": "done"}]})
        self._append({"type": "message", "role": "assistant", "phase": "final", "content": [{"type": "output_text", "text": "legacy done"}]})
        self._append({"type": "message", "role": "assistant", "phase": "analysis", "content": [{"type": "output_text", "text": "secret analysis"}]})
        env = {
            "CODEX_HOME": str(self.codex_home),
            "CODEX_THREAD_ID": self.thread_id,
            "CODEX_INTERNAL_ORIGINATOR_OVERRIDE": "Codex Desktop",
        }
        with patch.dict(os.environ, env):
            first = codex_desktop.sync("P")
            second = codex_desktop.sync("P")
        text = Path(af.conversations_dir("P"), "codex", "current.md").read_text()
        self.assertEqual(first["synced_messages"], 4)
        self.assertEqual(second["synced_messages"], 0)
        self.assertEqual(first["thread_ref"], self.thread_ref)
        self.assertNotIn(self.thread_id, json.dumps(first))
        self.assertIn("user request", text)
        self.assertIn("assistant/commentary", text)
        self.assertIn("assistant/final_answer", text)
        self.assertIn("assistant/final", text)
        for excluded in ("secret developer", "secret reasoning", "secret analysis"):
            self.assertNotIn(excluded, text)

    def test_scheduler_can_continue_from_checkpoint_without_environment_thread(self):
        self._append({"type": "message", "role": "user", "content": [{"type": "input_text", "text": "one"}]})
        with patch.dict(os.environ, {
            "CODEX_HOME": str(self.codex_home),
            "CODEX_THREAD_ID": self.thread_id,
            "CODEX_INTERNAL_ORIGINATOR_OVERRIDE": "Codex Desktop",
        }):
            codex_desktop.sync("P")
        self._append({"type": "message", "role": "assistant", "phase": "final_answer", "content": [{"type": "output_text", "text": "two"}]})
        with patch.dict(os.environ, {"CODEX_HOME": str(self.codex_home)}, clear=False):
            os.environ.pop("CODEX_THREAD_ID", None)
            result = codex_desktop.sync("P", allow_env=False)
        self.assertEqual(result["synced_messages"], 1)

    def test_cli_thread_cannot_register_or_replace_desktop_checkpoint(self):
        cli_thread_id = "cli-thread-456"
        cli_file = self.codex_home / "sessions" / "2026" / "07" / "14" / f"rollout-now-{cli_thread_id}.jsonl"
        cli_file.write_text(json.dumps({
            "timestamp": "2026-07-14T00:00:01Z",
            "type": "response_item",
            "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "cli poison"}]},
        }) + "\n", encoding="utf-8")
        cli_env = {
            "CODEX_HOME": str(self.codex_home),
            "CODEX_THREAD_ID": cli_thread_id,
            "CODEX_INTERNAL_ORIGINATOR_OVERRIDE": "Codex CLI",
        }
        with patch.dict(os.environ, cli_env, clear=True):
            unregistered = codex_desktop.sync("P")
        self.assertEqual(unregistered["status"], "not_found")
        self.assertIsNone(unregistered["thread_ref"])
        self.assertEqual(codex_desktop._load_checkpoint("P"), {})

        self._append({"type": "message", "role": "user", "content": [{"type": "input_text", "text": "desktop"}]})
        desktop_env = {
            "CODEX_HOME": str(self.codex_home),
            "CODEX_THREAD_ID": self.thread_id,
            "CODEX_INTERNAL_ORIGINATOR_OVERRIDE": "Codex Desktop",
        }
        with patch.dict(os.environ, desktop_env, clear=True):
            codex_desktop.sync("P")

        with patch.dict(os.environ, cli_env, clear=True):
            result = codex_desktop.sync("P")

        checkpoint = codex_desktop._load_checkpoint("P")
        text = Path(af.conversations_dir("P"), "codex", "current.md").read_text()
        self.assertEqual(checkpoint["thread_id"], self.thread_id)
        self.assertEqual(result["thread_ref"], self.thread_ref)
        self.assertNotIn("cli poison", text)
        self.assertNotIn(cli_thread_id, json.dumps(result))

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

        with patch.dict(os.environ, {
            "CODEX_HOME": str(self.codex_home),
            "CODEX_THREAD_ID": self.thread_id,
            "CODEX_INTERNAL_ORIGINATOR_OVERRIDE": "Codex Desktop",
        }):
            with redirect_stdout(io.StringIO()):
                af.cmd_submit("P", Args())
        state = af.load_state("P")
        self.assertEqual(state["task"]["owner"], "codex_cli")
        self.assertEqual(state["workflow"]["interaction"]["thread_ref"], self.thread_ref)
        self.assertNotIn(self.thread_id, json.dumps(state))


if __name__ == "__main__":
    unittest.main()

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

    def test_old_checkpoint_at_eof_backfills_only_missing_final_answer_once(self):
        self._append({"type": "message", "role": "user", "content": [{"type": "input_text", "text": "already saved user"}]})
        self._append({"type": "message", "role": "assistant", "phase": "commentary", "content": [{"type": "output_text", "text": "already saved commentary"}]})
        self._append({"type": "message", "role": "assistant", "phase": "final", "content": [{"type": "output_text", "text": "already saved legacy final"}]})
        self._append({"type": "message", "role": "assistant", "phase": "final_answer", "content": [{"type": "output_text", "text": "missing modern final"}]})
        current = Path(af.conversations_dir("P"), "codex", "current.md")
        current.write_text(
            "## Codex Desktop user — 2026-07-14T00:00:00Z\n\nalready saved user\n\n"
            "## Codex Desktop assistant/commentary — 2026-07-14T00:00:00Z\n\nalready saved commentary\n\n"
            "## Codex Desktop assistant/final — 2026-07-14T00:00:00Z\n\nalready saved legacy final\n",
            encoding="utf-8",
        )
        checkpoint = codex_desktop._checkpoint_path("P")
        checkpoint.parent.mkdir(parents=True, exist_ok=True)
        checkpoint.write_text(json.dumps({
            "thread_id": self.thread_id,
            "offset": self.thread_file.stat().st_size,
            "synced_at": "2026-07-14T00:01:00",
        }), encoding="utf-8")

        with patch.dict(os.environ, {"CODEX_HOME": str(self.codex_home)}, clear=False):
            first = codex_desktop.sync("P", allow_env=False)
            second = codex_desktop.sync("P", allow_env=False)

        text = current.read_text(encoding="utf-8")
        upgraded = codex_desktop._load_checkpoint("P")
        self.assertEqual(first["synced_messages"], 1)
        self.assertEqual(second["synced_messages"], 0)
        self.assertEqual(text.count("already saved user"), 1)
        self.assertEqual(text.count("already saved commentary"), 1)
        self.assertEqual(text.count("already saved legacy final"), 1)
        self.assertEqual(text.count("missing modern final"), 1)
        self.assertEqual(upgraded["schema_version"], codex_desktop._CHECKPOINT_SCHEMA_VERSION)
        self.assertEqual(upgraded["parser_version"], codex_desktop._PARSER_VERSION)

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

    def test_only_bound_desktop_thread_authorizes_human_control(self):
        self._append({"type": "message", "role": "user", "content": [{"type": "input_text", "text": "bind control"}]})
        desktop_env = {
            "CODEX_HOME": str(self.codex_home),
            "CODEX_THREAD_ID": self.thread_id,
            "CODEX_INTERNAL_ORIGINATOR_OVERRIDE": "Codex Desktop",
        }
        with patch.dict(os.environ, desktop_env, clear=True), \
             patch("plow_whip.codex_desktop._trusted_desktop_parent", return_value=True):
            self.assertTrue(codex_desktop.authorize_control("P", bind_if_missing=True))
            self.assertTrue(codex_desktop.authorize_control("P"))
        with patch.dict(os.environ, {
            **desktop_env,
            "CODEX_INTERNAL_ORIGINATOR_OVERRIDE": "Codex CLI",
        }, clear=True):
            self.assertFalse(codex_desktop.authorize_control("P"))
        with patch.dict(os.environ, {
            **desktop_env,
            "CODEX_THREAD_ID": "different-desktop-thread",
        }, clear=True), patch("plow_whip.codex_desktop._trusted_desktop_parent", return_value=True):
            self.assertFalse(codex_desktop.authorize_control("P"))

    def test_forged_desktop_environment_without_app_parent_is_denied(self):
        with patch.dict(os.environ, {
            "CODEX_THREAD_ID": self.thread_id,
            "CODEX_INTERNAL_ORIGINATOR_OVERRIDE": "Codex Desktop",
        }, clear=True), patch("plow_whip.codex_desktop._trusted_desktop_parent", return_value=False):
            self.assertFalse(codex_desktop.authorize_control("P", bind_if_missing=True))

    def test_desktop_process_lineage_allows_one_command_shell(self):
        app_server = "/Applications/ChatGPT.app/Contents/Resources/codex"
        app = "/Applications/ChatGPT.app/Contents/MacOS/ChatGPT"
        identities = {
            10: (20, "/bin/zsh"),
            20: (30, app_server),
            30: (1, app),
        }
        with patch("plow_whip.codex_desktop.os.getppid", return_value=10), \
             patch("plow_whip.codex_desktop._darwin_process", side_effect=lambda pid: identities.get(pid)):
            self.assertTrue(codex_desktop._trusted_desktop_parent())

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

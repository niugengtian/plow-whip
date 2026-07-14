"""Tests for plow-whip dispatch module — 鞭子本体."""

import json
import io
import os
import shutil
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch, MagicMock

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import plow_whip.agent_flow as af
import plow_whip.inbox_watcher as iw
from plow_whip import protocol
from plow_whip.agent_flow import save_config
from plow_whip.dispatch import (
    _dispatch_zellij,
    _dispatch_file,
    _dispatch_codex_cli,
    _dispatch_cursor_cli,
    _dispatch_notify,
    _ensure_inbox,
    available_channels,
    clear_inbox,
    dispatch,
    read_inbox,
    update_inbox_task,
    INBOX_DIR,
)


class DispatchTestBase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.projects_dir = os.path.join(self.tmpdir, "projects")
        os.makedirs(self.projects_dir)
        self.config_dir = os.path.join(self.tmpdir, "config")
        os.makedirs(self.config_dir)
        self.config_file = os.path.join(self.config_dir, "config.json")
        self.inbox_dir = os.path.join(self.tmpdir, "inbox")

        self._orig_config_file = af.CONFIG_FILE
        self._orig_config_dir = af.CONFIG_DIR
        af.CONFIG_FILE = self.config_file
        af.CONFIG_DIR = self.config_dir

        import plow_whip.dispatch as dp
        self._orig_inbox_dir = dp.INBOX_DIR
        dp.INBOX_DIR = self.inbox_dir
        self._orig_watcher_inbox_dir = iw.INBOX_DIR
        iw.INBOX_DIR = self.inbox_dir

        save_config({
            "projects_dir": self.projects_dir,
            "agents": ["cursor", "cursor_cli", "codex"],
        })

    def tearDown(self):
        af.CONFIG_FILE = self._orig_config_file
        af.CONFIG_DIR = self._orig_config_dir
        import plow_whip.dispatch as dp
        dp.INBOX_DIR = self._orig_inbox_dir
        iw.INBOX_DIR = self._orig_watcher_inbox_dir
        shutil.rmtree(self.tmpdir)


class TestDispatchFile(DispatchTestBase):
    def test_write_task_to_inbox(self):
        result = _dispatch_file("codex", "实现登录功能", "MyProject")
        self.assertTrue(result["success"])
        self.assertEqual(result["channel"], "file")

        tasks = read_inbox("codex")
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["project"], "MyProject")
        self.assertEqual(tasks[0]["prompt"], "实现登录功能")
        self.assertEqual(tasks[0]["status"], "queued")
        self.assertTrue(tasks[0]["dispatch_id"].startswith("DP-"))

    def test_append_multiple_tasks(self):
        _dispatch_file("codex", "Task 1", "P1")
        _dispatch_file("codex", "Task 2", "P2")
        tasks = read_inbox("codex")
        self.assertEqual(len(tasks), 2)
        self.assertEqual(tasks[0]["project"], "P1")
        self.assertEqual(tasks[1]["project"], "P2")

    def test_concurrent_dispatches_do_not_lose_inbox_records(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda index: _dispatch_file("codex", f"Task {index}", "P1"), range(20)))
        tasks = read_inbox("codex")
        self.assertEqual(len(tasks), 20)
        self.assertEqual(len({task["dispatch_id"] for task in tasks}), 20)

    def test_clear_inbox(self):
        _dispatch_file("codex", "Task", "P1")
        self.assertEqual(len(read_inbox("codex")), 1)
        clear_inbox("codex")
        self.assertEqual(len(read_inbox("codex")), 0)

    def test_read_empty_inbox(self):
        tasks = read_inbox("nonexistent")
        self.assertEqual(tasks, [])

    def test_agent_name_cannot_escape_inbox(self):
        with self.assertRaises(ValueError):
            _dispatch_file("../escape", "Task", "P1")

    def test_watcher_accepts_queued_task_once(self):
        _dispatch_file("codex", "Task", "P1")
        task = iw.accept_next_task("codex")
        self.assertEqual(task["status"], "accepted")
        self.assertIsNone(iw.accept_next_task("codex"))

    @patch("plow_whip.inbox_watcher.subprocess.run")
    def test_watcher_escapes_notification_text(self, mock_run):
        iw.send_notification('a"b', {"project": "P", "prompt": 'x"\\y'})
        script = mock_run.call_args.args[0][-1]
        self.assertIn('x\\"\\\\y', script)

    def test_update_one_dispatch_does_not_clear_others(self):
        first = _dispatch_file("codex", "One", "P1")
        second = _dispatch_file("codex", "Two", "P2")
        updated = update_inbox_task("codex", first["dispatch_id"], "completed", "done")
        self.assertEqual(updated["status"], "completed")
        tasks = read_inbox("codex")
        self.assertEqual(len(tasks), 2)
        self.assertEqual(tasks[1]["dispatch_id"], second["dispatch_id"])
        self.assertEqual(tasks[1]["status"], "queued")


class TestDispatchNotify(DispatchTestBase):
    @patch("plow_whip.dispatch.af.notify")
    def test_notify_sends_message(self, mock_notify):
        result = _dispatch_notify("cursor", "干活", "P1")
        self.assertTrue(result["success"])
        self.assertEqual(result["channel"], "notify")
        mock_notify.assert_called_once()


class TestDispatchMain(DispatchTestBase):
    @patch("plow_whip.dispatch.shutil.which", return_value="/bin/codex")
    @patch("plow_whip.dispatch.subprocess.Popen")
    def test_codex_exit_without_state_progress_is_failure(self, mock_popen, _mock_which):
        af.cmd_init("P1")
        workspace = os.path.join(self.config_dir, "worktrees", "P1", "T-work")
        os.makedirs(workspace)
        state = af.load_state("P1")
        state["workflow"] = {"git": {"workspace_ref": "P1/T-work"}}
        af.save_state("P1", state)
        process = mock_popen.return_value
        process.pid = 123
        process.returncode = 0
        process.stdout = io.StringIO('{"type":"thread.started","thread_id":"codex-session-1"}\n')
        process.wait.return_value = 0
        with patch.dict(os.environ, {
            "CODEX_THREAD_ID": "desktop-thread-must-not-leak",
            "CODEX_INTERNAL_ORIGINATOR_OVERRIDE": "Codex Desktop",
        }):
            result = _dispatch_codex_cli("work", "P1")
        self.assertFalse(result["success"])
        self.assertIn("任务状态未推进", result["detail"])
        session = af.load_state("P1")["task"]["active_session"]
        self.assertEqual(session["session_id"], "codex-session-1")
        command = mock_popen.call_args.args[0]
        child_env = mock_popen.call_args.kwargs["env"]
        self.assertNotIn("--ephemeral", command)
        self.assertIn("--skip-git-repo-check", command)
        self.assertEqual(
            os.path.realpath(command[command.index("-C") + 1]), os.path.realpath(workspace)
        )
        self.assertIn("--add-dir", command)
        self.assertEqual(
            command[command.index("--add-dir") + 1], af.project_collab_dir("P1")
        )
        self.assertNotIn("CODEX_THREAD_ID", child_env)
        self.assertEqual(child_env["CODEX_INTERNAL_ORIGINATOR_OVERRIDE"], "Codex CLI")

    @patch("plow_whip.dispatch.shutil.which", return_value="/bin/codex")
    @patch("plow_whip.dispatch.subprocess.Popen")
    def test_codex_existing_session_is_resumed(self, mock_popen, _mock_which):
        af.cmd_init("P1")
        state = af.load_state("P1")
        state["task"]["active_session"] = {
            "agent": "codex_cli",
            "session_id": "codex-session-1",
            "status": "active",
        }
        af.save_state("P1", state)
        process = mock_popen.return_value
        process.pid = 124
        process.returncode = 0
        process.stdout = io.StringIO('{"type":"thread.started","thread_id":"codex-session-1"}\n')
        process.wait.return_value = 0

        _dispatch_codex_cli("continue", "P1")
        command = mock_popen.call_args.args[0]
        self.assertIn("resume", command)
        self.assertIn("codex-session-1", command)

    @patch("plow_whip.dispatch.shutil.which", return_value="/bin/codex")
    @patch("plow_whip.dispatch.subprocess.Popen")
    def test_codex_pool_retries_retryable_failure_with_next_key_and_model(self, mock_popen, _mock_which):
        af.cmd_init("P1")
        cfg = af.load_config()
        cfg["cli_auth"] = {"codex_cli": {
            "mode": "pool", "active": "one", "auto_failover": True,
            "profiles": [
                {"name": "one", "env": "OPENAI_KEY_1", "model": "model-1", "enabled": True},
                {"name": "two", "env": "OPENAI_KEY_2", "model": "model-2", "enabled": True},
            ],
        }}
        af.save_config(cfg)
        first = MagicMock(pid=131, returncode=1, stdout=io.StringIO("Error 429 insufficient_quota\n"))
        first.wait.return_value = 1
        second = MagicMock(
            pid=132, returncode=0,
            stdout=io.StringIO('{"type":"thread.started","thread_id":"codex-pool-session"}\n'),
        )
        second.wait.return_value = 0
        mock_popen.side_effect = [first, second]
        with patch.dict(os.environ, {"OPENAI_KEY_1": "key-one", "OPENAI_KEY_2": "key-two"}):
            result = _dispatch_codex_cli("work", "P1")
        self.assertEqual(mock_popen.call_count, 2)
        self.assertEqual(mock_popen.call_args_list[0].kwargs["env"]["CODEX_API_KEY"], "key-one")
        self.assertEqual(mock_popen.call_args_list[1].kwargs["env"]["CODEX_API_KEY"], "key-two")
        self.assertIn("model-1", mock_popen.call_args_list[0].args[0])
        self.assertIn("model-2", mock_popen.call_args_list[1].args[0])
        self.assertNotIn("key-one", mock_popen.call_args_list[0].args[0])
        self.assertFalse(result["success"])

    @patch("plow_whip.dispatch.shutil.which", return_value="/bin/cursor-agent")
    @patch("plow_whip.dispatch.subprocess.Popen")
    def test_cursor_generated_session_is_recorded_and_resumed(self, mock_popen, _mock_which):
        af.cmd_init("P1")
        processes = []
        for pid, session_id in ((201, "cursor-session-1"), (202, "cursor-session-2")):
            process = MagicMock(pid=pid, returncode=0)
            process.stdout = io.StringIO(
                json.dumps({"type": "system", "subtype": "init", "session_id": session_id}) + "\n"
            )
            process.wait.return_value = 0
            processes.append(process)
        mock_popen.side_effect = processes

        first = _dispatch_cursor_cli("work", "P1")
        self.assertFalse(first["success"])
        session = af.load_state("P1")["task"]["active_session"]
        self.assertEqual(session["session_id"], "cursor-session-1")

        second = _dispatch_cursor_cli("continue", "P1")
        second_command = mock_popen.call_args.args[0]
        self.assertIn("--resume=cursor-session-1", second_command)
        self.assertIn("stream-json", second_command)
        self.assertFalse(second["success"])
        self.assertIn("unexpected session", second["detail"])

    @patch("plow_whip.dispatch.shutil.which", return_value="/bin/cursor-agent")
    @patch("plow_whip.dispatch.subprocess.Popen")
    def test_cursor_pool_retries_retryable_failure_with_next_key_and_model(self, mock_popen, _mock_which):
        af.cmd_init("P1")
        cfg = af.load_config()
        cfg["cli_auth"] = {"cursor_cli": {
            "mode": "pool", "active": "one", "auto_failover": True,
            "profiles": [
                {"name": "one", "env": "CURSOR_KEY_1", "model": "model-1", "enabled": True},
                {"name": "two", "env": "CURSOR_KEY_2", "model": "model-2", "enabled": True},
            ],
        }}
        af.save_config(cfg)
        first = MagicMock(pid=301, returncode=1, stdout=io.StringIO("Error 429 rate limit\n"))
        first.wait.return_value = 1
        second = MagicMock(
            pid=302, returncode=0,
            stdout=io.StringIO('{"type":"system","subtype":"init","session_id":"cursor-pool-session"}\n'),
        )
        second.wait.return_value = 0
        mock_popen.side_effect = [first, second]
        with patch.dict(os.environ, {"CURSOR_KEY_1": "key-one", "CURSOR_KEY_2": "key-two"}):
            result = _dispatch_cursor_cli("work", "P1")
        self.assertEqual(mock_popen.call_count, 2)
        self.assertEqual(mock_popen.call_args_list[0].kwargs["env"]["CURSOR_API_KEY"], "key-one")
        self.assertEqual(mock_popen.call_args_list[1].kwargs["env"]["CURSOR_API_KEY"], "key-two")
        self.assertIn("model-1", mock_popen.call_args_list[0].args[0])
        self.assertIn("model-2", mock_popen.call_args_list[1].args[0])
        self.assertNotIn("key-one", mock_popen.call_args_list[0].args[0])
        self.assertFalse(result["success"])

    @patch("plow_whip.dispatch.subprocess.run")
    def test_zellij_shell_quotes_untrusted_prompt(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        result = _dispatch_zellij("it's $(touch /tmp/nope)", "P one")
        self.assertTrue(result["success"])
        command = mock_run.call_args_list[-1].args[0][-1]
        self.assertIn("'\"'\"'", command)
        self.assertIn("'P one'", command)

    def test_strict_dispatch_rejects_zellij_and_closes_ledger(self):
        af.cmd_init("P1")
        result = dispatch("cursor_cli", "P1", "work", force_channel="zellij")
        self.assertFalse(result["success"])
        self.assertIn("legacy-only", result["detail"])
        self.assertEqual(read_inbox("cursor_cli")[0]["status"], "failed")

    @patch("plow_whip.dispatch._dispatch_notify")
    def test_direct_channel_keeps_shared_lifecycle_record(self, mock_notify):
        mock_notify.return_value = {"success": True, "channel": "notify", "detail": "OK"}
        result = dispatch("codex", "P1", "do something", force_channel="notify")
        tasks = read_inbox("codex")
        self.assertEqual(tasks[0]["dispatch_id"], result["dispatch_id"])
        self.assertEqual(tasks[0]["status"], "accepted")

    @patch("plow_whip.dispatch._zellij_available", return_value=False)
    @patch("plow_whip.dispatch._agent_cli_available")
    def test_cursor_desktop_does_not_use_cursor_cli_channel(self, mock_cli, _mock_zellij):
        mock_cli.side_effect = lambda agent: agent == "cursor_cli"
        self.assertNotIn("cursor_cli", available_channels("cursor"))
        self.assertIn("cursor_cli", available_channels("cursor_cli"))

    @patch("plow_whip.dispatch._agent_cli_available", return_value=True)
    def test_cli_agents_fall_back_to_each_other_before_file(self, _mock_cli):
        self.assertEqual(
            available_channels("cursor_cli")[:2],
            ["cursor_cli", "codex_cli"],
        )
        self.assertEqual(
            available_channels("codex_cli")[:2],
            ["codex_cli", "cursor_cli"],
        )

    @patch("plow_whip.dispatch._dispatch_codex_cli")
    @patch("plow_whip.dispatch._dispatch_cursor_cli")
    @patch(
        "plow_whip.dispatch.available_channels",
        return_value=["cursor_cli", "codex_cli", "file"],
    )
    def test_cursor_failure_is_executed_by_codex_before_file(
        self, _mock_channels, mock_cursor, mock_codex
    ):
        mock_cursor.return_value = {
            "success": False,
            "channel": "cursor_cli",
            "detail": "socket hang up",
        }
        mock_codex.return_value = {
            "success": True,
            "channel": "codex_cli",
            "detail": "completed",
        }

        result = dispatch("cursor_cli", "P1", "do something")

        self.assertEqual(result["channel"], "codex_cli")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["fallback_errors"][0]["channel"], "cursor_cli")

    @patch("plow_whip.dispatch._driver_available", return_value=True)
    @patch("plow_whip.dispatch._dispatch_codex_cli")
    @patch("plow_whip.dispatch._dispatch_cursor_cli")
    def test_generic_owner_fails_over_to_same_role_agent(
        self, mock_cursor, mock_codex, _mock_available
    ):
        af.cmd_init("P1")
        data = af.load_protocol("P1")
        data["agents"] = {
            "backend-primary": protocol.normalize_agent("backend-primary", {
                "roles": ["backend"], "capabilities": ["python"],
                "driver": "cursor_cli", "priority": 90,
            }),
            "backend-backup": protocol.normalize_agent("backend-backup", {
                "roles": ["backend"], "capabilities": ["python"],
                "driver": "codex_cli", "priority": 80,
            }),
            "reviewer": protocol.normalize_agent("reviewer", {
                "roles": ["reviewer"], "capabilities": ["review"],
                "driver": "codex_cli", "priority": 100,
            }),
        }
        protocol.save(af.project_dir("P1"), data)
        state = af.load_state("P1")
        state["task"].update({
            "owner": "backend-primary", "required_role": "backend",
            "required_capabilities": ["python"],
        })
        af.save_state("P1", state)
        mock_cursor.return_value = {"success": False, "channel": "cursor_cli", "detail": "aborted"}
        mock_codex.return_value = {"success": True, "channel": "codex_cli", "detail": "done"}

        result = dispatch("backend-primary", "P1", "work")

        self.assertEqual(result["logical_owner"], "backend-primary")
        self.assertEqual(result["executor"], "backend-backup")
        self.assertEqual(result["driver"], "codex_cli")
        execution = af.load_state("P1")["task"]["execution"]
        self.assertEqual(execution["logical_owner"], "backend-primary")
        self.assertEqual(execution["executor"], "backend-backup")
        self.assertEqual(execution["fallback_errors"][0]["channel"], "cursor_cli")

    @patch("plow_whip.dispatch.available_channels")
    @patch("plow_whip.dispatch._dispatch_file")
    def test_dispatch_falls_back_to_file(self, mock_file, mock_channels):
        mock_channels.return_value = ["file"]
        mock_file.return_value = {"success": True, "channel": "file", "detail": "OK"}

        result = dispatch("codex", "P1", "do something")
        self.assertTrue(result["success"])
        self.assertEqual(result["channel"], "file")

    @patch("plow_whip.dispatch._dispatch_codex_cli")
    @patch("plow_whip.dispatch.available_channels", return_value=["codex_cli", "file"])
    def test_fallback_preserves_cli_failure(self, _mock_channels, mock_codex):
        mock_codex.return_value = {"success": False, "channel": "codex_cli", "detail": "boom"}
        result = dispatch("codex", "P1", "do something")
        self.assertEqual(result["channel"], "file")
        self.assertEqual(result["fallback_errors"][0]["detail"], "boom")

    def test_dispatch_force_file(self):
        result = dispatch("codex", "P1", "do something", force_channel="file")
        self.assertTrue(result["success"])
        self.assertEqual(result["channel"], "file")

    @patch("plow_whip.dispatch._dispatch_file")
    def test_dispatch_all_fail(self, mock_file):
        mock_file.return_value = {"success": False, "channel": "file", "detail": "fail"}
        result = dispatch("codex", "P1", "do something", force_channel="file")
        self.assertFalse(result["success"])
        self.assertEqual(result["detail"], "file: fail")
        self.assertEqual(result["failures"], [{"channel": "file", "detail": "fail"}])


class FakeArgs:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


if __name__ == "__main__":
    unittest.main()

"""Tests for plow-whip dispatch module — 鞭子本体."""

import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch, MagicMock

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import plow_whip.agent_flow as af
from plow_whip.agent_flow import save_config
from plow_whip.dispatch import (
    _dispatch_file,
    _dispatch_notify,
    _ensure_inbox,
    available_channels,
    clear_inbox,
    dispatch,
    read_inbox,
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

        save_config({
            "projects_dir": self.projects_dir,
            "agents": ["qoder", "codex", "cursor"],
        })

    def tearDown(self):
        af.CONFIG_FILE = self._orig_config_file
        af.CONFIG_DIR = self._orig_config_dir
        import plow_whip.dispatch as dp
        dp.INBOX_DIR = self._orig_inbox_dir
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
        self.assertEqual(tasks[0]["status"], "pending")

    def test_append_multiple_tasks(self):
        _dispatch_file("codex", "Task 1", "P1")
        _dispatch_file("codex", "Task 2", "P2")
        tasks = read_inbox("codex")
        self.assertEqual(len(tasks), 2)
        self.assertEqual(tasks[0]["project"], "P1")
        self.assertEqual(tasks[1]["project"], "P2")

    def test_clear_inbox(self):
        _dispatch_file("codex", "Task", "P1")
        self.assertEqual(len(read_inbox("codex")), 1)
        clear_inbox("codex")
        self.assertEqual(len(read_inbox("codex")), 0)

    def test_read_empty_inbox(self):
        tasks = read_inbox("nonexistent")
        self.assertEqual(tasks, [])


class TestDispatchNotify(DispatchTestBase):
    @patch("plow_whip.dispatch.af.notify")
    def test_notify_sends_message(self, mock_notify):
        result = _dispatch_notify("qoder", "干活", "P1")
        self.assertTrue(result["success"])
        self.assertEqual(result["channel"], "notify")
        mock_notify.assert_called_once()


class TestDispatchMain(DispatchTestBase):
    @patch("plow_whip.dispatch.available_channels")
    @patch("plow_whip.dispatch._dispatch_file")
    def test_dispatch_falls_back_to_file(self, mock_file, mock_channels):
        mock_channels.return_value = ["file"]
        mock_file.return_value = {"success": True, "channel": "file", "detail": "OK"}

        result = dispatch("codex", "P1", "do something")
        self.assertTrue(result["success"])
        self.assertEqual(result["channel"], "file")

    def test_dispatch_force_file(self):
        result = dispatch("codex", "P1", "do something", force_channel="file")
        self.assertTrue(result["success"])
        self.assertEqual(result["channel"], "file")

    @patch("plow_whip.dispatch._dispatch_file")
    def test_dispatch_all_fail(self, mock_file):
        mock_file.return_value = {"success": False, "channel": "file", "detail": "fail"}
        result = dispatch("codex", "P1", "do something", force_channel="file")
        self.assertFalse(result["success"])


class FakeArgs:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


if __name__ == "__main__":
    unittest.main()

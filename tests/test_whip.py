"""Tests for plow-whip whip module — 上帝之鞭."""

import json
import os
import shutil
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import plow_whip.agent_flow as af
from plow_whip.whip import (
    STALE_THRESHOLD_MINUTES,
    _is_stale,
    _parse_updated_at,
    _staleness_info,
    _wake_hash,
    _wake_lease_active,
    cmd_whip,
    filter_active,
    filter_by_agent,
    generate_notification,
    generate_whip_prompt,
    scan_all_projects,
)
from plow_whip.agent_flow import save_config


class WhipTestBase(unittest.TestCase):
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


class TestStaleDetection(WhipTestBase):
    def test_parse_valid_timestamp(self):
        dt = _parse_updated_at("2026-06-30T12:00:00")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.year, 2026)

    def test_parse_empty_timestamp(self):
        self.assertIsNone(_parse_updated_at(""))
        self.assertIsNone(_parse_updated_at(None))

    def test_parse_invalid_timestamp(self):
        self.assertIsNone(_parse_updated_at("not-a-date"))

    def test_is_stale_never_updated(self):
        state = {"status": "in_progress", "updated_at": ""}
        self.assertTrue(_is_stale(state, 60))

    def test_is_stale_recently_updated(self):
        now = datetime.now().astimezone()
        state = {"status": "in_progress", "updated_at": now.isoformat()}
        self.assertFalse(_is_stale(state, 60))

    def test_is_stale_old_update(self):
        old = datetime.now().astimezone() - timedelta(hours=2)
        state = {"status": "in_progress", "updated_at": old.isoformat()}
        self.assertTrue(_is_stale(state, 60))

    def test_is_stale_done_not_stale(self):
        old = datetime.now().astimezone() - timedelta(days=7)
        state = {"status": "done", "updated_at": old.isoformat()}
        self.assertFalse(_is_stale(state, 60))

    def test_is_stale_blocked_not_stale(self):
        old = datetime.now().astimezone() - timedelta(days=7)
        state = {"status": "blocked", "updated_at": old.isoformat()}
        self.assertFalse(_is_stale(state, 60))


class TestStalenessInfo(WhipTestBase):
    def test_never_updated(self):
        state = {"updated_at": ""}
        self.assertEqual(_staleness_info(state), "从未更新")

    def test_seconds_ago(self):
        now = datetime.now().astimezone()
        state = {"updated_at": now.isoformat()}
        info = _staleness_info(state)
        self.assertIn("秒前", info)

    def test_minutes_ago(self):
        old = datetime.now().astimezone() - timedelta(minutes=15)
        state = {"updated_at": old.isoformat()}
        info = _staleness_info(state)
        self.assertIn("分钟前", info)

    def test_hours_ago(self):
        old = datetime.now().astimezone() - timedelta(hours=3)
        state = {"updated_at": old.isoformat()}
        info = _staleness_info(state)
        self.assertIn("小时前", info)

    def test_days_ago(self):
        old = datetime.now().astimezone() - timedelta(days=5)
        state = {"updated_at": old.isoformat()}
        info = _staleness_info(state)
        self.assertIn("天前", info)


class TestWakeLease(unittest.TestCase):
    def test_queued_wake_retries_after_one_scheduler_cycle(self):
        wake_hash = "same-action"
        state = {
            "last_wake_hash": wake_hash,
            "last_wake_status": "queued",
            "last_woken_at": (
                datetime.now().astimezone() - timedelta(seconds=61)
            ).isoformat(),
        }
        self.assertFalse(_wake_lease_active(state, wake_hash))

    def test_accepted_wake_keeps_normal_duplicate_lease(self):
        wake_hash = "same-action"
        state = {
            "last_wake_hash": wake_hash,
            "last_wake_status": "accepted",
            "last_woken_at": (
                datetime.now().astimezone() - timedelta(minutes=5)
            ).isoformat(),
        }
        self.assertTrue(_wake_lease_active(state, wake_hash))


class TestScanAllProjects(WhipTestBase):
    def test_empty_projects(self):
        results = scan_all_projects()
        self.assertEqual(results, [])

    def test_scan_single_project(self):
        af.cmd_init("TestProject")
        results = scan_all_projects()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["project"], "TestProject")
        self.assertEqual(results[0]["current_agent"], "qoder")
        self.assertEqual(results[0]["project_path"], os.path.join(self.projects_dir, "TestProject"))
        self.assertFalse(results[0]["stale"])  # 刚初始化，updated_at=now → not stale

    def test_scan_multiple_projects(self):
        af.cmd_init("ProjectA")
        af.cmd_init("ProjectB")
        results = scan_all_projects()
        self.assertEqual(len(results), 2)
        names = [r["project"] for r in results]
        self.assertIn("ProjectA", names)
        self.assertIn("ProjectB", names)

    def test_done_project_still_scanned(self):
        af.cmd_init("DoneProject")
        # 手动标记为 done
        state = af.load_state("DoneProject")
        state["task"]["status"] = "done"
        af.save_state("DoneProject", state)
        results = scan_all_projects()
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]["stale"])  # done 不算 stale


class TestFilters(WhipTestBase):
    def test_filter_by_agent(self):
        af.cmd_init("P1")
        af.cmd_init("P2")
        # P2 handoff to codex
        args = FakeArgs(
            output="Done", next="Next", phase="P", status="done",
            day=None, topic=None, project_dir=None, files=None, verify=None,
        )
        af.cmd_handoff("P2", args)
        results = scan_all_projects()
        qoder_results = filter_by_agent(results, "qoder")
        codex_results = filter_by_agent(results, "codex")
        self.assertEqual(len(qoder_results), 1)
        self.assertEqual(qoder_results[0]["project"], "P1")
        self.assertEqual(len(codex_results), 1)
        self.assertEqual(codex_results[0]["project"], "P2")

    def test_filter_active(self):
        af.cmd_init("Active")
        af.cmd_init("Done")
        state = af.load_state("Done")
        state["task"]["status"] = "done"
        af.save_state("Done", state)
        results = scan_all_projects()
        active = filter_active(results)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["project"], "Active")

    def test_filter_active_excludes_blocked_projects(self):
        results = [
            {"project": "Active", "status": "in_progress"},
            {"project": "Blocked", "status": "blocked"},
            {"project": "Done", "status": "done"},
        ]
        self.assertEqual([item["project"] for item in filter_active(results)], ["Active"])


class TestWhipPrompt(WhipTestBase):
    def test_generate_whip_prompt(self):
        result = {
            "project": "TestProject",
            "current_agent": "codex",
            "phase": "Sprint 1",
            "next_action": "实现登录功能",
            "staleness_info": "2小时前",
            "task_context": {"day": 3, "topic": "认证", "project_dir": "src/"},
        }
        prompt = generate_whip_prompt(result)
        self.assertIn("TestProject", prompt)
        self.assertIn("codex", prompt)
        self.assertIn("实现登录功能", prompt)
        self.assertIn("2小时前", prompt)
        self.assertIn("Day 3", prompt)

    def test_generate_whip_prompt_points_to_collab_files(self):
        result = {
            "project": "TestProject",
            "current_agent": "codex",
            "phase": "Sprint 1",
            "next_action": "实现登录功能",
            "project_path": os.path.join(self.projects_dir, "TestProject"),
            "staleness_info": "2小时前",
            "task_context": {},
        }
        prompt = generate_whip_prompt(result)
        self.assertIn("start --agent codex --json", prompt)
        self.assertNotIn("collab/AGENT_COMMS.md", prompt)

    def test_generate_notification_stale(self):
        result = {
            "project": "P1",
            "current_agent": "qoder",
            "stale": True,
            "next_action": "继续开发",
        }
        msg = generate_notification(result)
        self.assertIn("P1", msg)
        self.assertIn("qoder", msg)
        self.assertIn("摸鱼", msg)

    def test_generate_notification_fresh(self):
        result = {
            "project": "P1",
            "current_agent": "qoder",
            "stale": False,
            "next_action": "继续开发",
        }
        msg = generate_notification(result)
        self.assertNotIn("摸鱼", msg)


class TestCmdWhip(WhipTestBase):
    @patch("plow_whip.whip.run_once")
    def test_legacy_daemon_flags_run_once(self, mock_once):
        mock_once.return_value = {"status": "ok", "projects": []}
        cmd_whip(FakeArgs(agent=None, stale_minutes=60, json=False, daemon=True, interval=300,
                          crack=False, auto_crack=False, channel=None, force=False,
                          auto_rotate=False, brain=False, once=False))
        mock_once.assert_called_once()

    def test_whip_no_projects(self):
        args = FakeArgs(agent=None, stale_minutes=60, json=False, daemon=False, interval=300)
        cmd_whip(args)  # Should not raise

    def test_whip_with_project(self):
        af.cmd_init("TestProject")
        args = FakeArgs(agent=None, stale_minutes=60, json=False, daemon=False, interval=300)
        cmd_whip(args)  # Should print report

    def test_whip_json_output(self):
        af.cmd_init("TestProject")
        args = FakeArgs(agent=None, stale_minutes=60, json=True, daemon=False, interval=300)
        cmd_whip(args)  # Should print JSON

    def test_whip_target_agent(self):
        af.cmd_init("P1")
        af.cmd_init("P2")
        args = FakeArgs(agent="codex", stale_minutes=60, json=False, daemon=False, interval=300)
        cmd_whip(args)  # Should only show codex projects

    def test_whip_all_done(self):
        af.cmd_init("Done1")
        state = af.load_state("Done1")
        state["task"]["status"] = "done"
        af.save_state("Done1", state)
        args = FakeArgs(agent=None, stale_minutes=60, json=False, daemon=False, interval=300)
        cmd_whip(args)  # Should print "all done"

    @patch("plow_whip.whip.dispatch")
    def test_crack_does_not_dispatch_same_task_twice(self, mock_dispatch):
        mock_dispatch.return_value = {"success": True, "channel": "file", "detail": "OK"}
        af.cmd_init("TestProject")
        state = af.load_state("TestProject")
        state["updated_at"] = (datetime.now().astimezone() - timedelta(hours=2)).isoformat()
        af.write_state("TestProject", state, touch=False)

        args = FakeArgs(
            agent=None, stale_minutes=60, json=False, daemon=False, interval=300,
            crack=True, auto_crack=False, channel="file", force=False,
            auto_rotate=False, brain=False,
        )
        cmd_whip(args)
        cmd_whip(args)

        self.assertEqual(mock_dispatch.call_count, 1)
        state = af.load_state("TestProject")
        self.assertEqual(state["wake_count"], 1)
        self.assertTrue(state["last_wake_hash"])

        state["last_woken_at"] = (datetime.now().astimezone() - timedelta(hours=1)).isoformat()
        af.write_state("TestProject", state, touch=False)
        cmd_whip(args)
        self.assertEqual(mock_dispatch.call_count, 2)
        self.assertEqual(af.load_state("TestProject")["wake_count"], 2)

        state = af.load_state("TestProject")
        state["wake_count"] = 3
        state["last_woken_at"] = (datetime.now().astimezone() - timedelta(hours=1)).isoformat()
        af.write_state("TestProject", state, touch=False)
        cmd_whip(args)
        self.assertEqual(mock_dispatch.call_count, 2)

    @patch("plow_whip.whip.dispatch")
    def test_queued_cli_wake_gets_additional_failover_attempts(self, mock_dispatch):
        mock_dispatch.return_value = {
            "success": True,
            "channel": "codex_cli",
            "detail": "recovered",
            "status": "completed",
        }
        af.cmd_init("TestProject")
        state = af.load_state("TestProject")
        state["updated_at"] = (
            datetime.now().astimezone() - timedelta(hours=2)
        ).isoformat()
        wake_hash = _wake_hash(scan_all_projects()[0])
        state["last_wake_hash"] = wake_hash
        state["last_wake_status"] = "queued"
        state["last_woken_at"] = (
            datetime.now().astimezone() - timedelta(minutes=2)
        ).isoformat()
        state["wake_count"] = 3
        af.write_state("TestProject", state, touch=False)

        args = FakeArgs(
            agent=None, stale_minutes=60, json=False, daemon=False, interval=300,
            crack=True, auto_crack=False, channel=None, force=False,
            auto_rotate=False, brain=False,
        )
        cmd_whip(args)

        mock_dispatch.assert_called_once()
        self.assertEqual(af.load_state("TestProject")["wake_count"], 4)


class FakeArgs:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


if __name__ == "__main__":
    unittest.main()

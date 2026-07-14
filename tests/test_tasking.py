import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

import plow_whip.agent_flow as af
from plow_whip import protocol, tasking


class TaskingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.projects = os.path.join(self.tmp, "projects")
        self.config = os.path.join(self.tmp, "config")
        os.makedirs(self.projects)
        os.makedirs(self.config)
        self.old_file, self.old_dir = af.CONFIG_FILE, af.CONFIG_DIR
        af.CONFIG_DIR = self.config
        af.CONFIG_FILE = os.path.join(self.config, "config.json")
        af.save_config({"projects_dir": self.projects, "agents": ["codex", "codex_cli", "cursor_cli"]})
        af.cmd_init("P")

    def tearDown(self):
        af.CONFIG_FILE, af.CONFIG_DIR = self.old_file, self.old_dir
        shutil.rmtree(self.tmp)

    def test_local_classifier_is_conservative(self):
        direct = tasking.classify_task("驱动 cursor cli 对代码进行一次审查")
        self.assertEqual(direct["route"], "direct")
        self.assertEqual(direct["driver"], "cursor_cli")
        self.assertFalse(direct["code_change"])
        self.assertFalse(
            tasking.classify_task("驱动 cursor cli 只读审查当前改动，禁止修改任何文件")["code_change"]
        )
        self.assertFalse(
            tasking.classify_task("Use cursor cli to review the diff; do not modify files")["code_change"]
        )
        self.assertTrue(
            tasking.classify_task("驱动 cursor cli 检查代码并修复发现的问题")["code_change"]
        )
        self.assertEqual(
            tasking.classify_task("帮我写个钱包地址余额监控并超过阈值时通过 TG 群告警")["route"],
            "simple",
        )
        self.assertEqual(tasking.classify_task("给我写个 XXX 平台")["route"], "needs_planner")
        self.assertEqual(tasking.classify_task("优化一下")["route"], "needs_planner")

    def test_default_planner_ignores_legacy_goal_planner(self):
        data = af.load_protocol("P")
        data["agents"]["goal-planner"] = protocol.normalize_agent("goal-planner", {
            "roles": ["planner"], "capabilities": ["*"], "driver": "cursor_cli", "priority": 100,
        })
        protocol.save(af.project_dir("P"), data)
        self.assertEqual(tasking.planner_owner(data), "codex_cli")

    def test_planner_plan_must_wait_for_human_confirmation(self):
        payload = tasking.submit("P", "给我设计并实现一个完整支付平台")
        self.assertEqual(payload["workflow"]["status"], "planning")
        self.assertEqual(payload["task"]["owner"], "codex_cli")
        plan = [
            {"title": "Build", "role": "implementation", "acceptance": ["built"]},
            {"title": "Review", "role": "reviewer", "acceptance": ["approved"], "final_acceptance": True},
        ]
        proposed = tasking.propose_plan("P", "two stages", plan)
        self.assertEqual(proposed["task"]["status"], "blocked_waiting_human")
        self.assertEqual(proposed["workflow"]["status"], "awaiting_confirmation")
        with patch("plow_whip.tasking.git_flow.prepare_branch", return_value={"branch": "plow/t", "target_branch": "main"}):
            confirmed = tasking.confirm_plan("P")
        self.assertEqual(confirmed["workflow"]["status"], "active")
        self.assertEqual(confirmed["task"]["title"], "Build")
        self.assertEqual(len(confirmed["workflow"]["queue"]), 1)

    def test_simple_task_creates_branch_and_independent_review(self):
        with patch("plow_whip.tasking.git_flow.prepare_branch", return_value={"branch": "plow/simple", "target_branch": "main"}):
            payload = tasking.submit("P", "写一个余额阈值告警脚本")
        self.assertEqual(payload["classification"]["route"], "simple")
        self.assertEqual(payload["task"]["owner"], "simple-tasker")
        review = payload["workflow"]["queue"][0]
        self.assertNotEqual(review["owner"], "simple-tasker")
        with open(os.path.join(self.projects, "P", "collab", "AGENT_PROTOCOL.json"), encoding="utf-8") as file:
            self.assertIn("simple-tasker", json.load(file)["agents"])

    def test_sandboxed_verification_uses_the_simple_tasker_result(self):
        with patch(
            "plow_whip.simple_tasker.SimpleTasker.run_command",
            return_value={"returncode": 0, "output": "checks passed"},
        ):
            results = af.run_task_verification(
                "P", ["python3 -m unittest"],
                {"id": "T-simple", "verification_policy": "sandboxed"},
            )
        self.assertEqual(results, [{
            "command": "python3 -m unittest", "returncode": 0, "output": "checks passed",
        }])

    def test_reviewer_rejection_resumes_original_executor_session(self):
        state = af.load_state("P")
        implementation = {
            "id": "T-X", "title": "Build", "owner": "cursor_cli", "status": "done", "stage": "implementation",
            "next_action": "", "acceptance": [], "verify_commands": [], "rule_tags": [], "last_output": "built",
            "blockers": [], "decision_ids": [], "cli_sessions": {"cursor_cli": {"session_id": "cursor-1"}},
        }
        review = {
            "id": "T-X-REVIEW", "title": "Review", "owner": "codex_cli", "status": "active", "stage": "review",
            "next_action": "review", "acceptance": [], "verify_commands": [], "rule_tags": [], "last_output": "",
            "blockers": [], "decision_ids": [], "cli_sessions": {"codex_cli": {"session_id": "codex-review-1"}},
        }
        state["workflow"] = {"id": "T-X", "status": "active", "queue": [], "last_implementation": implementation}
        state["task"] = review
        af.save_state("P", state)
        payload = tasking.reject_review("P", "missing edge case")
        self.assertEqual(payload["task"]["owner"], "cursor_cli")
        self.assertEqual(payload["task"]["cli_sessions"]["cursor_cli"]["session_id"], "cursor-1")
        self.assertEqual(payload["workflow"]["queue"][0]["cli_sessions"], {})

    def test_decision_request_revokes_execution_and_answer_resumes_same_task(self):
        state = af.load_state("P")
        state["task"].update({"placeholder": False, "status": "active"})
        state["workflow"] = {"id": "T-X", "status": "active", "source": "current_session"}
        af.save_state("P", state)
        requested = tasking.request_decision(
            "P", "Choose storage", ["SQLite", "PostgreSQL"], "SQLite",
        )
        self.assertEqual(requested["task"]["status"], "blocked_waiting_human")
        answered = tasking.answer_decision("P", "1", "small local workload")
        self.assertEqual(answered["decision"]["choice"], "SQLite")
        self.assertEqual(answered["task"]["status"], "active")
        self.assertIn(answered["decision"]["id"], answered["task"]["decision_ids"])


if __name__ == "__main__":
    unittest.main()

import os
import shutil
import subprocess
import tempfile
import unittest

from plow_whip import git_flow


def run(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


class GitFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.remote = os.path.join(self.tmp, "remote.git")
        self.repo = os.path.join(self.tmp, "repo")
        run(self.tmp, "init", "--bare", "-q", self.remote)
        run(self.tmp, "init", "-q", "-b", "main", self.repo)
        run(self.repo, "config", "user.name", "Test")
        run(self.repo, "config", "user.email", "test@example.com")
        with open(os.path.join(self.repo, "app.txt"), "w", encoding="utf-8") as file:
            file.write("one\n")
        run(self.repo, "add", "app.txt")
        run(self.repo, "commit", "-qm", "init")
        run(self.repo, "remote", "add", "origin", self.remote)
        run(self.repo, "push", "-u", "origin", "main")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_reviewed_branch_fast_forwards_remote_target(self):
        state = git_flow.prepare_branch(self.repo, "T-100", "main")
        with open(os.path.join(self.repo, "app.txt"), "w", encoding="utf-8") as file:
            file.write("two\n")
        delivered = git_flow.finalize_fast_forward(self.repo, "T-100", state)
        remote_head = run(self.repo, "ls-remote", "origin", "refs/heads/main").split()[0]
        self.assertEqual(remote_head, delivered["commit"])
        self.assertTrue(delivered["merged"])

    def test_target_movement_blocks_instead_of_rebasing(self):
        state = git_flow.prepare_branch(self.repo, "T-200", "main")
        with open(os.path.join(self.repo, "app.txt"), "w", encoding="utf-8") as file:
            file.write("task change\n")
        other = os.path.join(self.tmp, "other")
        run(self.tmp, "clone", "-q", "-b", "main", self.remote, other)
        run(other, "config", "user.name", "Other")
        run(other, "config", "user.email", "other@example.com")
        with open(os.path.join(other, "remote.txt"), "w", encoding="utf-8") as file:
            file.write("remote moved\n")
        run(other, "add", "remote.txt")
        run(other, "commit", "-qm", "remote move")
        run(other, "push", "origin", "main")
        with self.assertRaisesRegex(git_flow.GitFlowBlocked, "cannot fast-forward"):
            git_flow.finalize_fast_forward(self.repo, "T-200", state)

    def test_strict_workspace_cannot_push_and_publisher_only_pushes_task_branch(self):
        workspace = os.path.join(self.tmp, "worktrees", "T-300")
        state = git_flow.prepare_workspace(self.repo, workspace, "T-300", "main")
        self.assertEqual(run(self.repo, "branch", "--show-current"), "main")
        self.assertEqual(run(workspace, "branch", "--show-current"), state["branch"])
        self.assertEqual(run(workspace, "config", "--worktree", "--get", "remote.origin.pushurl"), "disabled://plow-whip-worker")
        with open(os.path.join(workspace, "app.txt"), "w", encoding="utf-8") as file:
            file.write("strict task\n")
        commit = git_flow.checkpoint_branch(workspace, "T-300", state)
        delivered = git_flow.publish_reviewed(
            workspace, "T-300", state, commit, auto_merge=False, publisher_path=self.repo,
        )
        self.assertEqual(delivered["status"], "awaiting_human_merge")
        self.assertFalse(delivered["merged"])
        remote_main = run(self.repo, "ls-remote", "origin", "refs/heads/main").split()[0]
        self.assertNotEqual(remote_main, commit)
        remote_task = run(self.repo, "ls-remote", "origin", f"refs/heads/{state['branch']}").split()[0]
        self.assertEqual(remote_task, commit)
        run(self.repo, "merge", "--ff-only", commit)
        run(self.repo, "push", "origin", "main")
        self.assertTrue(git_flow.remote_contains(self.repo, "main", commit))

    def test_workspace_and_branch_names_resist_slug_collisions(self):
        self.assertNotEqual(
            git_flow.workspace_path(self.tmp, "a b", "T-1"),
            git_flow.workspace_path(self.tmp, "a-b", "T-1"),
        )
        self.assertNotEqual(
            git_flow.workspace_path(self.tmp, "项目甲", "T 1"),
            git_flow.workspace_path(self.tmp, "项目乙", "T-1"),
        )
        self.assertNotEqual(git_flow._branch_name("T 1"), git_flow._branch_name("T-1"))

    def test_existing_workspace_must_belong_to_the_same_repository(self):
        workspace = os.path.join(self.tmp, "worktrees", "collision")
        git_flow.prepare_workspace(self.repo, workspace, "T-collision", "main")
        other = os.path.join(self.tmp, "other-repo")
        run(self.tmp, "clone", "-q", "-b", "main", self.remote, other)
        with self.assertRaisesRegex(git_flow.GitFlowBlocked, "another repository"):
            git_flow.prepare_workspace(other, workspace, "T-collision", "main")


if __name__ == "__main__":
    unittest.main()

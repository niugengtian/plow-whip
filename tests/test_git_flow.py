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


if __name__ == "__main__":
    unittest.main()

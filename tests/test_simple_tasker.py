import json
import os
import shutil
import subprocess
import tempfile
import unittest

from plow_whip.simple_tasker import SandboxViolation, SimpleTasker


class FakeClient:
    def __init__(self, messages):
        self.messages = list(messages)
        self.last_key_ref = "deepseek/01/****abcd/fp-12345678"

    def chat(self, _messages, tools=None, max_tokens=4000):
        return self.messages.pop(0)


class SimpleTaskerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.tmp, "collab", "memory", "sessions"))
        subprocess.run(["git", "init", "-q", self.tmp], check=True)
        with open(os.path.join(self.tmp, "value.txt"), "w", encoding="utf-8") as file:
            file.write("old\n")
        subprocess.run(["git", "-C", self.tmp, "add", "value.txt"], check=True)
        subprocess.run(["git", "-C", self.tmp, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "init"], check=True)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_tool_loop_persists_and_archives_session(self):
        patch_text = "--- a/value.txt\n+++ b/value.txt\n@@ -1 +1 @@\n-old\n+new\n"
        client = FakeClient([
            {"role": "assistant", "content": None, "tool_calls": [{
                "id": "c1", "type": "function", "function": {"name": "apply_patch", "arguments": json.dumps({"patch": patch_text})},
            }]},
            {"role": "assistant", "content": None, "tool_calls": [{
                "id": "c2", "type": "function", "function": {"name": "run_command", "arguments": json.dumps({"command": "python3 -m compileall value.txt"})},
            }]},
            {"role": "assistant", "content": json.dumps({"status": "completed", "summary": "changed value", "verify_commands": ["python3 -m compileall value.txt"]})},
        ])
        runner = SimpleTasker(self.tmp, "T-1", client=client)
        result = runner.run("change value")
        self.assertEqual(result["status"], "completed")
        with open(os.path.join(self.tmp, "value.txt"), encoding="utf-8") as file:
            self.assertEqual(file.read(), "new\n")
        with open(runner.session_path, encoding="utf-8") as file:
            session = file.read()
        self.assertIn("apply_patch", session)
        self.assertNotIn("secret", session)
        self.assertEqual(runner.archive()["status"], "archived")

    def test_session_is_resumed_without_duplicate_user_task(self):
        first = SimpleTasker(self.tmp, "T-2", client=FakeClient([
            {"role": "assistant", "content": json.dumps({"status": "completed", "summary": "one", "verify_commands": []})},
        ]))
        first.run("one task")
        second = SimpleTasker(self.tmp, "T-2", client=FakeClient([
            {"role": "assistant", "content": json.dumps({"status": "completed", "summary": "resume", "verify_commands": []})},
        ]))
        second.run("one task")
        users = [event for event in second._events() if event.get("type") == "message" and event.get("message", {}).get("role") == "user"]
        self.assertEqual(len(users), 1)

    def test_sandbox_rejects_secrets_escape_and_git_lifecycle(self):
        runner = SimpleTasker(self.tmp, "T-3", client=FakeClient([]))
        with self.assertRaises(SandboxViolation):
            runner.read_file("../outside")
        with self.assertRaises(SandboxViolation):
            runner.read_file(".env")
        with self.assertRaises(SandboxViolation):
            runner.run_command("git push origin main")

    def test_child_commands_do_not_receive_api_key_environment_variables(self):
        probe = os.path.join(self.tmp, "env_probe.py")
        with open(probe, "w", encoding="utf-8") as file:
            file.write("import os\nprint(os.getenv('DEEPSEEK_API_KEY', 'missing'))\n")
        runner = SimpleTasker(self.tmp, "T-env", client=FakeClient([]))
        with unittest.mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "never-log-this"}):
            result = runner.run_command("python3 env_probe.py")
        self.assertEqual(result["returncode"], 0)
        self.assertEqual(result["output"].strip(), "missing")

    def test_long_context_is_compacted_without_deleting_jsonl_history(self):
        runner = SimpleTasker(
            self.tmp, "T-4", context_limit_chars=80,
            client=FakeClient([{"role": "assistant", "content": "task summary and next action"}]),
        )
        runner._append({"type": "message", "message": {"role": "user", "content": "x" * 200}})
        before = os.path.getsize(runner.session_path)
        runner._maybe_compact()
        self.assertGreater(os.path.getsize(runner.session_path), before)
        self.assertEqual(runner._events()[-1]["type"], "summary")
        self.assertIn("Persisted session summary", runner._messages()[1]["content"])


if __name__ == "__main__":
    unittest.main()

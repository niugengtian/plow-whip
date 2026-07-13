"""Tests for rotation enforcement."""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import plow_whip.agent_flow as af
from plow_whip import rotation as rot
from plow_whip.agent_flow import cmd_init, enforce_project_rotation, build_rotation_health


class RotationTestBase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.projects_dir = os.path.join(self.tmpdir, "projects")
        os.makedirs(self.projects_dir)
        self.config_dir = os.path.join(self.tmpdir, "config")
        os.makedirs(self.config_dir)
        self.config_file = os.path.join(self.config_dir, "config.json")
        self._orig_config_file = af.CONFIG_FILE
        self._orig_config_dir = af.CONFIG_DIR
        af.CONFIG_FILE = self.config_file
        af.CONFIG_DIR = self.config_dir
        af.save_config({"projects_dir": self.projects_dir, "agents": ["codex", "cursor"]})

    def tearDown(self):
        af.CONFIG_FILE = self._orig_config_file
        af.CONFIG_DIR = self._orig_config_dir
        shutil.rmtree(self.tmpdir)


class TestCommsBlockRotation(RotationTestBase):
    def test_framework_messages_are_blocks_and_auto_rotate(self):
        cmd_init("TestProject")
        for i in range(8):
            af.append_comms("TestProject", f"system message {i}")
        comms = af.comms_file("TestProject")
        self.assertLessEqual(rot.comms_block_count(comms), rot.COMMS_KEEP_RECENT_BLOCKS)
        with open(comms, encoding="utf-8") as f:
            content = f.read()
            self.assertIn("### [system]", content)
            self.assertLessEqual(content.count("<!-- Previous messages archived to:"), 1)
        archives = [name for name in os.listdir(os.path.join(af.project_memory_dir("TestProject"), "sessions")) if name.endswith(".md")]
        self.assertGreaterEqual(len(archives), 3)

    def test_rotation_compacts_accumulated_archive_pointers(self):
        cmd_init("TestProject")
        comms = af.comms_file("TestProject")
        with open(comms, encoding="utf-8") as f:
            body = f.read()
        pointers = "\n\n".join(
            f"<!-- Previous messages archived to: /tmp/archive-{index}.md -->" for index in range(50)
        )
        with open(comms, "w", encoding="utf-8") as f:
            f.write(pointers + "\n\n" + body)

        summary = enforce_project_rotation("TestProject")

        self.assertIn("AGENT_COMMS.md", summary["files"])
        with open(comms, encoding="utf-8") as f:
            compacted = f.read()
        self.assertEqual(compacted.count("<!-- Previous messages archived to:"), 1)
        self.assertFalse(summary["health"]["needs_enforcement"])

    def test_archive_comms_keeps_recent_blocks(self):
        cmd_init("TestProject")
        comms = af.comms_file("TestProject")
        blocks = []
        for i in range(8):
            blocks.append(f"### [codex] 2026-07-12 — Message {i}\n\nBody {i}\n")
        with open(comms, "w", encoding="utf-8") as f:
            f.write("# Agent Message Board\n\n---\n\n## Recent Messages\n\n")
            f.write("\n".join(blocks))

        archive_dir = os.path.join(af.project_memory_dir("TestProject"), "sessions")
        archived, path = rot.archive_comms_by_blocks(
            "TestProject", comms, archive_dir, keep_blocks=5
        )
        self.assertTrue(archived)
        self.assertTrue(os.path.exists(path))

        with open(comms, encoding="utf-8") as f:
            kept = f.read()
        self.assertIn("Message 7", kept)
        self.assertNotIn("Message 0", kept)
        self.assertIn("archived to:", kept)

        _, remaining = rot.split_message_blocks(kept)
        self.assertEqual(len(remaining), 5)

    def test_enforce_project_rotation_archives_oversized_comms(self):
        cmd_init("TestProject")
        comms = af.comms_file("TestProject")
        with open(comms, "w", encoding="utf-8") as f:
            f.write("# board\n\n")
            for i in range(10):
                f.write(f"### [codex] 2026-07-12 — Msg {i}\n\ncontent\n\n")

        health = build_rotation_health("TestProject")
        self.assertTrue(health["needs_enforcement"])

        summary = enforce_project_rotation("TestProject")
        self.assertIn("AGENT_COMMS.md", summary["files"])
        self.assertTrue(summary["health"]["collab_files"][0]["blocks"] <= rot.COMMS_KEEP_RECENT_BLOCKS)

    def test_enforce_rotates_five_large_blocks_to_fit_line_budget(self):
        cmd_init("TestProject")
        comms = af.comms_file("TestProject")
        with open(comms, "w", encoding="utf-8") as f:
            f.write("# board\n\n")
            for i in range(rot.COMMS_KEEP_RECENT_BLOCKS):
                body = "\n".join(f"line {n}" for n in range(20))
                f.write(f"### [codex] 2026-07-12 — Msg {i}\n\n{body}\n\n")

        summary = enforce_project_rotation("TestProject")

        self.assertIn("AGENT_COMMS.md", summary["files"])
        self.assertFalse(summary["health"]["needs_enforcement"])


class FakeArgs:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


if __name__ == "__main__":
    unittest.main()

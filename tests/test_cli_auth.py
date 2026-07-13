import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

import plow_whip.agent_flow as af
from plow_whip import cli_auth


class CliAuthTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.old_file = af.CONFIG_FILE
        af.CONFIG_FILE = os.path.join(self.tmpdir, "config.json")
        af.save_config({"projects_dir": self.tmpdir, "agents": ["codex_cli", "cursor_cli"]})

    def tearDown(self):
        af.CONFIG_FILE = self.old_file
        shutil.rmtree(self.tmpdir)

    def test_defaults_to_desktop_with_empty_pool(self):
        self.assertEqual(cli_auth.candidates("codex_cli"), [{"name": "desktop", "model": None, "secret": None}])
        self.assertEqual(cli_auth.get("cursor_cli")["profiles"], [])

    def test_pool_orders_active_profile_and_never_exposes_secret_in_status(self):
        cli_auth.add("codex_cli", "first", "OPENAI_KEY_1", "model-1")
        cli_auth.add("codex_cli", "second", "OPENAI_KEY_2", "model-2")
        cli_auth.select("codex_cli", "second")
        cli_auth.set_mode("codex_cli", "pool")
        with patch.dict(os.environ, {"OPENAI_KEY_1": "secret-1", "OPENAI_KEY_2": "secret-2"}):
            self.assertEqual([item["name"] for item in cli_auth.candidates("codex_cli")], ["second", "first"])
            self.assertNotIn("secret", str(cli_auth.safe_status("codex_cli")))

    def test_empty_pool_has_no_candidate(self):
        cli_auth.set_mode("cursor_cli", "pool")
        self.assertEqual(cli_auth.candidates("cursor_cli"), [])


if __name__ == "__main__":
    unittest.main()

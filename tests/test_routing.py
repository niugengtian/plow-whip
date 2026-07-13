"""Role/capability routing tests."""

import unittest

from plow_whip import routing


class RoutingTest(unittest.TestCase):
    def setUp(self):
        self.protocol = {
            "agents": {
                "backend-primary": {
                    "roles": ["backend"], "capabilities": ["python", "api"],
                    "driver": "codex_cli", "priority": 80, "cost_tier": "medium", "enabled": True,
                },
                "backend-backup": {
                    "roles": ["backend"], "capabilities": ["python", "api"],
                    "driver": "cursor_cli", "priority": 60, "cost_tier": "low", "enabled": True,
                },
                "reviewer": {
                    "roles": ["reviewer"], "capabilities": ["review"],
                    "driver": "cursor_cli", "priority": 90, "cost_tier": "low", "enabled": True,
                },
            }
        }

    def test_selects_highest_priority_eligible_agent(self):
        selected = routing.select_agent(
            self.protocol, role="backend", capabilities=["python", "api"],
        )
        self.assertEqual(selected, "backend-primary")

    def test_excludes_failed_driver_and_keeps_same_role(self):
        selected = routing.select_agent(
            self.protocol, role="backend", capabilities=["python"],
            exclude_drivers={"codex_cli"},
        )
        self.assertEqual(selected, "backend-backup")

    def test_execution_routes_never_cross_role(self):
        routes = routing.execution_routes(self.protocol, "backend-primary", lambda driver: True)
        self.assertEqual([item["agent"] for item in routes], ["backend-primary", "backend-backup"])

    def test_compact_registry_omits_unneeded_context(self):
        self.protocol["agents"]["backend-primary"]["assignment"] = "long private assignment"
        compact = routing.compact_registry(self.protocol)
        self.assertNotIn("assignment", compact[0])


if __name__ == "__main__":
    unittest.main()

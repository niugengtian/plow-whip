"""Deterministic role/capability routing over the canonical agent registry."""

from __future__ import annotations


COST_SCORE = {"low": 20, "medium": 10, "high": 0}


def candidates(
    protocol: dict,
    role: str | None = None,
    capabilities: list[str] | None = None,
    exclude_agents: set[str] | None = None,
    exclude_drivers: set[str] | None = None,
    driver_available=None,
) -> list[dict]:
    required = set(capabilities or [])
    excluded_agents = exclude_agents or set()
    excluded_drivers = exclude_drivers or set()
    found = []
    for order, (agent, meta) in enumerate(protocol.get("agents", {}).items()):
        driver = meta.get("driver", "file")
        roles = set(meta.get("roles") or [])
        agent_capabilities = set(meta.get("capabilities") or [])
        if not meta.get("enabled", True) or agent in excluded_agents or driver in excluded_drivers:
            continue
        if role and role not in roles:
            continue
        if required and "*" not in agent_capabilities and not required.issubset(agent_capabilities):
            continue
        if driver_available and not driver_available(driver):
            continue
        found.append({
            "agent": agent,
            "driver": driver,
            "roles": sorted(roles),
            "capabilities": sorted(agent_capabilities),
            "priority": int(meta.get("priority", 50)),
            "cost_tier": meta.get("cost_tier", "medium"),
            "order": order,
        })
    return sorted(
        found,
        key=lambda item: (-item["priority"], -COST_SCORE.get(item["cost_tier"], 0), item["order"], item["agent"]),
    )


def select_agent(protocol: dict, **requirements) -> str | None:
    matches = candidates(protocol, **requirements)
    return matches[0]["agent"] if matches else None


def execution_routes(
    protocol: dict,
    owner: str,
    driver_available=None,
    role: str | None = None,
    capabilities: list[str] | None = None,
) -> list[dict]:
    meta = protocol.get("agents", {}).get(owner, {})
    roles = list(meta.get("roles") or [])
    role = role or (roles[0] if roles else None)
    capabilities = list(capabilities if capabilities is not None else (meta.get("capabilities") or []))
    primary_driver = meta.get("driver", "file")
    routes = []
    if meta.get("enabled", True) and (not driver_available or driver_available(primary_driver)):
        routes.append({
            "agent": owner, "driver": primary_driver, "roles": roles,
            "capabilities": list(meta.get("capabilities") or []),
            "priority": int(meta.get("priority", 50)),
            "cost_tier": meta.get("cost_tier", "medium"), "order": -1,
        })
    routes += candidates(
        protocol,
        role=role,
        capabilities=capabilities,
        exclude_agents={owner},
        driver_available=driver_available,
    )
    unique = []
    seen_drivers = set()
    for route in routes:
        if route["driver"] in seen_drivers:
            continue
        seen_drivers.add(route["driver"])
        unique.append(route)
    return unique


def compact_registry(protocol: dict) -> list[dict]:
    """Small planner view; omit assignments and provider/auth details."""
    return [
        {
            "agent": agent,
            "roles": meta.get("roles", []),
            "capabilities": meta.get("capabilities", []),
            "driver": meta.get("driver", "file"),
            "priority": meta.get("priority", 50),
            "cost_tier": meta.get("cost_tier", "medium"),
        }
        for agent, meta in protocol.get("agents", {}).items()
        if meta.get("enabled", True)
    ]


def planner_catalog(protocol: dict) -> dict:
    """Constant-shape planner view: unique tags only, never the Agent list."""
    registry = compact_registry(protocol)
    return {
        "roles": sorted({role for item in registry for role in item["roles"]}),
        "capabilities": sorted({cap for item in registry for cap in item["capabilities"] if cap != "*"}),
        "plan_fields": ["title", "role", "capabilities", "acceptance", "verify_commands", "final_acceptance"],
        "note": "Prefer role/capabilities; specify owner only when the user requires one exact agent.",
    }

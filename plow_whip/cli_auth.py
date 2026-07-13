"""CLI authentication profiles. Config stores references, never secret values."""

from __future__ import annotations

import json
import os
import re

from . import agent_flow as af


CLI_AGENTS = ("codex_cli", "cursor_cli")
_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")


def _default() -> dict:
    return {"mode": "desktop", "active": None, "auto_failover": True, "profiles": []}


def get(agent: str) -> dict:
    if agent not in CLI_AGENTS:
        raise ValueError(f"unsupported CLI agent: {agent}")
    value = {**_default(), **af.load_config().get("cli_auth", {}).get(agent, {})}
    value["profiles"] = list(value.get("profiles") or [])
    return value


def _save(agent: str, value: dict) -> dict:
    cfg = af.load_config()
    cfg.setdefault("cli_auth", {})[agent] = value
    af.save_config(cfg)
    return value


def set_mode(agent: str, mode: str) -> dict:
    if mode not in ("desktop", "pool"):
        raise ValueError("mode must be desktop or pool")
    value = get(agent)
    value["mode"] = mode
    return _save(agent, value)


def add(agent: str, name: str, env: str, model: str | None = None) -> dict:
    if not name or any(item.get("name") == name for item in get(agent)["profiles"]):
        raise ValueError("profile name must be non-empty and unique")
    if not _ENV_NAME.fullmatch(env):
        raise ValueError("env must be an uppercase environment variable name")
    value = get(agent)
    value["profiles"].append({"name": name, "env": env, "model": model or None, "enabled": True})
    value["active"] = value["active"] or name
    return _save(agent, value)


def remove(agent: str, name: str) -> dict:
    value = get(agent)
    value["profiles"] = [item for item in value["profiles"] if item.get("name") != name]
    if value["active"] == name:
        value["active"] = value["profiles"][0]["name"] if value["profiles"] else None
    return _save(agent, value)


def select(agent: str, name: str) -> dict:
    value = get(agent)
    if not any(item.get("name") == name for item in value["profiles"]):
        raise ValueError(f"unknown profile: {name}")
    value["active"] = name
    return _save(agent, value)


def set_failover(agent: str, enabled: bool) -> dict:
    value = get(agent)
    value["auto_failover"] = enabled
    return _save(agent, value)


def candidates(agent: str) -> list[dict]:
    value = get(agent)
    if value["mode"] == "desktop":
        return [{"name": "desktop", "model": None, "secret": None}]
    profiles = [item for item in value["profiles"] if item.get("enabled", True)]
    profiles.sort(key=lambda item: item.get("name") != value.get("active"))
    ready = [
        {"name": item["name"], "model": item.get("model"), "secret": os.environ.get(item["env"])}
        for item in profiles if os.environ.get(item.get("env", ""))
    ]
    return ready if value["auto_failover"] else ready[:1]


def safe_status(agent: str) -> dict:
    value = get(agent)
    return {
        **value,
        "profiles": [
            {**item, "available": bool(os.environ.get(item.get("env", "")))}
            for item in value["profiles"]
        ],
    }


def cmd(args) -> None:
    try:
        if args.action == "status":
            payload = {agent: safe_status(agent) for agent in ([args.agent] if args.agent else CLI_AGENTS)}
        elif args.action == "mode":
            payload = set_mode(args.agent, args.mode)
        elif args.action == "add":
            payload = add(args.agent, args.name, args.env, args.model)
        elif args.action == "remove":
            payload = remove(args.agent, args.name)
        elif args.action == "select":
            payload = select(args.agent, args.name)
        else:
            payload = set_failover(args.agent, args.value == "on")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    except ValueError as exc:
        raise SystemExit(f"Error: {exc}") from exc

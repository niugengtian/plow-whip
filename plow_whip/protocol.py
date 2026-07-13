"""Canonical machine protocol and derived human handbook."""

from __future__ import annotations

import json
import hashlib
import os

from .io_utils import atomic_write_json, atomic_write_text


PROTOCOL_NAME = "AGENT_PROTOCOL.json"
HANDBOOK_NAME = "HANDBOOK.zh-CN.md"

DEFAULT_ROLES = {
    "codex": "Codex Desktop (Coordinator)",
    "codex_cli": "Codex CLI (Code Owner)",
    "cursor": "Cursor Desktop",
    "cursor_cli": "Cursor CLI",
    "reviewer": "Reviewer",
}

DEFAULT_AGENT_ROUTING = {
    "codex": {"roles": ["planner", "coordinator", "reviewer"], "capabilities": ["*"], "driver": "codex_cli", "priority": 60},
    "codex_cli": {"roles": ["planner", "implementation", "reviewer"], "capabilities": ["*"], "driver": "codex_cli", "priority": 50},
    "cursor": {"roles": ["implementation", "reviewer"], "capabilities": ["*"], "driver": "zellij"},
    "cursor_cli": {"roles": ["planner", "implementation", "reviewer"], "capabilities": ["*"], "driver": "cursor_cli", "priority": 70, "cost_tier": "low"},
    "reviewer": {"roles": ["reviewer"], "capabilities": ["review"], "driver": "file"},
}

EXECUTION_DRIVERS = {"codex_cli", "cursor_cli", "zellij", "file"}
COST_TIERS = {"low", "medium", "high"}


def normalize_role(value: str) -> str:
    cleaned = "-".join(value.lower().replace("_", "-").split())
    return "".join(ch for ch in cleaned if ch.isalnum() or ch == "-").strip("-")


def normalize_agent(agent: str, meta: dict | None = None) -> dict:
    meta = dict(meta or {})
    defaults = DEFAULT_AGENT_ROUTING.get(agent, {})
    role_label = meta.get("role") or DEFAULT_ROLES.get(agent, agent)
    roles = meta.get("roles") or defaults.get("roles") or [normalize_role(role_label) or agent]
    capabilities = meta.get("capabilities")
    if capabilities is None:
        capabilities = defaults.get("capabilities", [])
    driver = meta.get("driver") or defaults.get("driver", "file")
    cost_tier = meta.get("cost_tier", defaults.get("cost_tier", "medium"))
    if driver not in EXECUTION_DRIVERS:
        raise ValueError(f"unsupported driver for {agent}: {driver}")
    if cost_tier not in COST_TIERS:
        raise ValueError(f"unsupported cost tier for {agent}: {cost_tier}")
    return {
        **meta,
        "role": role_label,
        "roles": list(dict.fromkeys(normalize_role(str(item)) for item in roles if normalize_role(str(item)))),
        "capabilities": list(dict.fromkeys(str(item).strip().lower() for item in capabilities if str(item).strip())),
        "driver": driver,
        "priority": int(meta.get("priority", defaults.get("priority", 50))),
        "cost_tier": cost_tier,
        "assignment": meta.get("assignment", ""),
        "enabled": meta.get("enabled", True),
    }


def normalize_agents(data: dict) -> bool:
    changed = False
    for agent, meta in list(data.setdefault("agents", {}).items()):
        normalized = normalize_agent(agent, meta)
        if normalized != meta:
            data["agents"][agent] = normalized
            changed = True
    return changed

GLOBAL_RULES = {
    "R001": {
        "summary": "Run plow-whip start as the only startup entry.",
        "summary_zh": "只通过 plow-whip start 进入项目。",
        "locked": True,
        "scope": ["startup"],
    },
    "R002": {
        "summary": "Never use rm; move removals into project-local by_rm.",
        "summary_zh": "禁止 rm；删除内容移动到项目内 by_rm。",
        "locked": True,
        "scope": ["filesystem"],
    },
    "R003": {
        "summary": "Stay inside the project root unless the user explicitly authorizes another path.",
        "summary_zh": "除非用户明确授权，否则只读写当前项目根目录。",
        "locked": True,
        "scope": ["filesystem"],
    },
    "R004": {
        "summary": "Do not delegate to subagents unless the user explicitly authorizes it.",
        "summary_zh": "除非用户明确授权，否则禁止委派子智能体。",
        "locked": True,
        "scope": ["delegation"],
    },
    "R005": {
        "summary": "Update work through task or handoff commands; AGENT_STATE.json is runtime truth.",
        "summary_zh": "通过 task 或 handoff 更新工作；AGENT_STATE.json 是运行状态真源。",
        "locked": False,
        "scope": ["workflow"],
    },
    "R006": {
        "summary": "Load Hot state every startup, Warm items only when targeted, and search Cold history on demand.",
        "summary_zh": "启动只加载 Hot；Warm 仅加载定向信息；Cold 历史按需搜索。",
        "locked": False,
        "scope": ["memory"],
    },
    "R007": {
        "summary": "Automatically resume only stale active tasks; never dispatch done or blocked work, and retry unchanged work after a bounded lease.",
        "summary_zh": "自动续作仅处理超时的 active 任务；done/blocked 永不派发，未进展任务在有限租约后才重试。",
        "locked": False,
        "scope": ["automation"],
    },
    "R008": {
        "summary": "Each CLI owns at most one generated session per task; resume it until completion, then archive it.",
        "summary_zh": "每个 CLI 在一个任务中最多绑定一个自身生成的会话；持续恢复该会话，任务完成后归档。",
        "locked": False,
        "scope": ["sessions", "automation"],
    },
    "R009": {
        "summary": "A task with verification commands is done only after every command passes; failures remain active for repair.",
        "summary_zh": "配置了验收命令的任务只有全部通过才算完成；失败时保持 active 并继续修复。",
        "locked": False,
        "scope": ["workflow", "verification", "automation"],
    },
}

REQUIRED_GLOBAL_RULES = {"R001", "R002", "R003", "R004", "R005", "R009"}
RULE_ENFORCEMENT = {
    "R001": "block", "R002": "block", "R003": "require_approval",
    "R004": "require_approval", "R005": "block", "R009": "verify",
}


def normalize_rule(rule_id: str, rule: dict, scope: str) -> dict:
    """Add the four rule dimensions while preserving legacy protocol files."""
    normalized = dict(rule)
    legacy_domains = normalized.get("scope") if isinstance(normalized.get("scope"), list) else []
    normalized["scope"] = scope
    normalized.setdefault("priority", "required" if scope == "project" or rule_id in REQUIRED_GLOBAL_RULES else "important")
    normalized.setdefault("origin", "inherited" if scope == "global" else "local")
    normalized.setdefault("enforcement", RULE_ENFORCEMENT.get(rule_id, "warn" if normalized["priority"] == "important" else "block"))
    applies_to = normalized.setdefault("applies_to", {})
    if legacy_domains:
        applies_to.setdefault("domains", legacy_domains)
    applies_to.setdefault("agents", ["*"])
    return normalized


def normalize_rules(data: dict) -> bool:
    changed = False
    for key, scope in (("global_rules", "global"), ("project_rules", "project")):
        rules = data.setdefault(key, {})
        for rule_id, rule in list(rules.items()):
            normalized = normalize_rule(rule_id, rule, scope)
            if normalized != rule:
                rules[rule_id] = normalized
                changed = True
    return changed


def compiled_rules(data: dict, agent: str, task: dict | None = None) -> dict:
    """Compile truth-source rules into the small per-agent startup view."""
    required, important = [], []
    task_tags = set((task or {}).get("rule_tags", []))
    effective = effective_rules(data)
    for rule_id, rule in effective.items():
        agents = rule.get("applies_to", {}).get("agents", ["*"])
        if "*" not in agents and agent not in agents:
            continue
        applies_to = rule.get("applies_to", {})
        required_tags = set(applies_to.get("task_tags", []))
        if rule["priority"] == "important" and not required_tags:
            required_tags = set(applies_to.get("domains", []))
        if required_tags and not required_tags.intersection(task_tags):
            continue
        derived = {
            "id": rule_id,
            "summary": rule.get("summary", ""),
            "scope": rule["scope"],
            "priority": rule["priority"],
            "origin": "derived",
            "derived_from": [rule_id],
            "source_origin": rule["origin"],
            "enforcement": rule["enforcement"],
        }
        (required if rule["priority"] == "required" else important).append(derived)
    encoded = json.dumps([required, important], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "mandatory_rules": required,
        "important_rules": important,
        "required_context": [
            {"rule_id": item["id"], "details_ref": effective[item["id"]]["details_ref"]}
            for item in important if effective[item["id"]].get("details_ref")
        ],
        "rules_meta": {
            "schema_version": data.get("schema_version", 3),
            "effective_hash": hashlib.sha256(encoded.encode()).hexdigest()[:16],
        },
    }


def protocol_path(project_dir: str) -> str:
    return os.path.join(project_dir, "collab", PROTOCOL_NAME)


def handbook_path(project_dir: str) -> str:
    return os.path.join(project_dir, "collab", HANDBOOK_NAME)


def default_protocol(project: str, agents: list[str], agent_meta: dict | None = None) -> dict:
    agent_meta = agent_meta or {}
    return {
        "schema_version": 4,
        "project": project,
        "global_rules": {rule_id: normalize_rule(rule_id, rule, "global") for rule_id, rule in GLOBAL_RULES.items()},
        "project_rules": {},
        "agents": {
            agent: normalize_agent(agent, agent_meta.get(agent, {}))
            for agent in agents
        },
        "memory": {
            "hot": ["AGENT_STATE.json"],
            "machine": ["AGENT_PROTOCOL.json"],
            "warm": ["targeted_messages", "current_handoff"],
            "cold": ["memory/DECISIONS.md", "memory/sessions", "conversations/*/archives"],
        },
        "startup": "plow-whip --project {project} start --agent {agent} --json",
    }


def load(project_dir: str) -> dict:
    with open(protocol_path(project_dir), encoding="utf-8") as f:
        return json.load(f)


def save(project_dir: str, data: dict) -> None:
    atomic_write_json(protocol_path(project_dir), data, indent=None)


def ensure(project_dir: str, project: str, agents: list[str], agent_meta: dict | None = None) -> dict:
    path = protocol_path(project_dir)
    if os.path.exists(path):
        data = load(project_dir)
        changed = False
        if data.get("schema_version", 1) < 4:
            data["schema_version"] = 4
            changed = True
        global_rules = data.setdefault("global_rules", {})
        for rule_id, rule in GLOBAL_RULES.items():
            if rule_id not in global_rules:
                global_rules[rule_id] = normalize_rule(rule_id, rule, "global")
                changed = True
        changed = normalize_rules(data) or changed
        changed = normalize_agents(data) or changed
        memory = data.setdefault("memory", {})
        hot = memory.setdefault("hot", ["AGENT_STATE.json"])
        if "AGENT_PROTOCOL.json" in hot:
            memory["hot"] = [item for item in hot if item != "AGENT_PROTOCOL.json"]
            changed = True
        if memory.setdefault("machine", ["AGENT_PROTOCOL.json"]) != ["AGENT_PROTOCOL.json"]:
            memory["machine"] = ["AGENT_PROTOCOL.json"]
            changed = True
        if changed:
            save(project_dir, data)
            write_handbook(project_dir, data)
        return data
    data = default_protocol(project, agents, agent_meta)
    save(project_dir, data)
    write_handbook(project_dir, data)
    return data


def enabled_agents(data: dict) -> list[str]:
    agents = [name for name, meta in data.get("agents", {}).items() if meta.get("enabled", True)]
    for name in agents:
        if not name.strip() or name in (".", "..") or "/" in name or "\\" in name or "\x00" in name:
            raise ValueError(f"invalid agent identifier: {name!r}")
    return agents


def effective_rules(data: dict) -> dict:
    rules = dict(data.get("global_rules", {}))
    for rule_id, rule in data.get("project_rules", {}).items():
        overridden = rule.get("overrides")
        if overridden:
            base = rules.get(overridden, {})
            if base.get("locked"):
                raise ValueError(f"project rule {rule_id} cannot override locked rule {overridden}")
            rules.pop(overridden, None)
        rules[rule_id] = rule
    return rules


def render_handbook(data: dict) -> str:
    lines = [
        "# 多 Agent 协作手册",
        "",
        "> 本文件由 `AGENT_PROTOCOL.json` 单向生成，只供人类阅读，请勿手改。",
        "",
        "## 全局原则",
        "",
    ]
    for rule_id, rule in data.get("global_rules", {}).items():
        lock = "（不可覆盖）" if rule.get("locked") else ""
        meta = f"{rule.get('priority', 'required')}/{rule.get('origin', 'inherited')}/{rule.get('enforcement', 'block')}"
        lines.append(f"- **{rule_id}**{lock} `{meta}`：{rule.get('summary_zh', rule.get('summary', ''))}")
    lines += ["", "## 项目原则", ""]
    project_rules = data.get("project_rules", {})
    if project_rules:
        for rule_id, rule in project_rules.items():
            meta = f"{rule.get('priority', 'required')}/{rule.get('origin', 'local')}/{rule.get('enforcement', 'block')}"
            lines.append(f"- **{rule_id}** `{meta}`：{rule.get('summary_zh', rule.get('summary', ''))}")
    else:
        lines.append("- 当前无额外项目原则。")
    lines += ["", "## Agent 阵容", "", "| Agent | Roles | Driver | Capabilities | Assignment |", "|---|---|---|---|---|"]
    for agent, meta in data.get("agents", {}).items():
        if meta.get("enabled", True):
            roles = ", ".join(meta.get("roles") or [meta.get("role", agent)])
            capabilities = ", ".join(meta.get("capabilities") or []) or "—"
            lines.append(f"| `{agent}` | {roles} | {meta.get('driver', 'file')} | {capabilities} | {meta.get('assignment') or '—'} |")
    lines.append("")
    return "\n".join(lines)


def write_handbook(project_dir: str, data: dict | None = None) -> bool:
    data = data or load(project_dir)
    path = handbook_path(project_dir)
    rendered = render_handbook(data)
    old = ""
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            old = f.read()
    if old == rendered:
        return False
    atomic_write_text(path, rendered)
    return True

"""Canonical machine protocol and derived human handbook."""

from __future__ import annotations

import json
import hashlib
import os
import secrets

from .io_utils import atomic_write_json, atomic_write_text


PROTOCOL_NAME = "AGENT_PROTOCOL.json"
HANDBOOK_NAME = "HANDBOOK.zh-CN.md"

DEFAULT_ROLES = {
    "codex": "Codex Desktop (Coordinator)",
    "codex_cli": "Codex CLI (Code Owner)",
    "cursor": "Cursor Desktop",
    "cursor_cli": "Cursor CLI",
    "reviewer": "Reviewer",
    "simple-tasker": "Simple Tasker (DeepSeek)",
}

DEFAULT_AGENT_ROUTING = {
    "codex": {"roles": ["control-plane"], "capabilities": ["human-interaction"], "driver": "control", "priority": 60, "schedulable": False},
    "codex_cli": {"roles": ["planner", "implementation", "reviewer"], "capabilities": ["*"], "driver": "codex_cli", "priority": 50},
    "cursor": {"roles": ["control-plane"], "capabilities": ["human-interaction"], "driver": "control", "schedulable": False},
    "cursor_cli": {"roles": ["planner", "implementation", "reviewer"], "capabilities": ["*"], "driver": "cursor_cli", "priority": 70, "cost_tier": "low"},
    "reviewer": {"roles": ["reviewer"], "capabilities": ["review"], "driver": "file"},
    "simple-tasker": {
        "roles": ["implementation"], "capabilities": ["simple-task"],
        "driver": "simple_tasker", "priority": 90, "cost_tier": "low",
    },
}

EXECUTION_DRIVERS = {"codex_cli", "cursor_cli", "simple_tasker", "zellij", "file", "control"}
COST_TIERS = {"low", "medium", "high"}

DEFAULT_ORCHESTRATION = {
    "default_planner": "codex_cli",
    "default_target_branch": "main",
    "scheduler_interval_seconds": 60,
    "max_concurrency_per_driver": 5,
    "retry_limit": 3,
    "circuit_recovery_successes": 3,
    "lease_ttl_seconds": 2400,
    "auto_merge_protected": False,
}


def normalize_role(value: str) -> str:
    cleaned = "-".join(value.lower().replace("_", "-").split())
    return "".join(ch for ch in cleaned if ch.isalnum() or ch == "-").strip("-")


def normalize_agent(agent: str, meta: dict | None = None) -> dict:
    meta = dict(meta or {})
    defaults = DEFAULT_AGENT_ROUTING.get(agent, {})
    if agent == "codex":
        meta.update({
            "role": "Codex Desktop (Control Plane)",
            "roles": defaults["roles"],
            "capabilities": defaults["capabilities"],
            "driver": defaults["driver"],
            "schedulable": False,
        })
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
        "schedulable": meta.get("schedulable", defaults.get("schedulable", True)),
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
        "summary": "Every Agent work session enters through plow-whip start; task intake uses submit and scheduled continuation uses whip --once.",
        "summary_zh": "每个 Agent 工作会话只通过 plow-whip start 进入；任务入口使用 submit，定时续作使用 whip --once。",
        "locked": True,
        "scope": ["startup"],
    },
    "R002": {
        "summary": "Agents never delete project content with rm or unlink; move removals into project-local by_rm. Framework cleanup is limited to its own ephemeral runtime and scheduler artifacts.",
        "summary_zh": "Agent 禁止用 rm 或 unlink 删除项目内容，删除项移入项目内 by_rm；框架仅可清理自身临时运行文件与定时任务产物。",
        "locked": True,
        "scope": ["filesystem"],
    },
    "R003": {
        "summary": "Agent file operations stay inside the project root unless the user explicitly authorizes another path; framework-owned config, runtime, and native scheduler files are the only infrastructure exception.",
        "summary_zh": "除非用户明确授权，Agent 文件操作只能位于当前项目根目录；仅框架自有配置、运行状态与原生定时任务文件属于基础设施例外。",
        "locked": True,
        "scope": ["filesystem"],
    },
    "R004": {
        "summary": "Do not create or delegate ephemeral nested subagents unless the user explicitly authorizes it; Registry/Router/Task/Handoff routing among registered plow-whip Agents is allowed.",
        "summary_zh": "除非用户明确授权，禁止创建或委派临时嵌套子智能体；允许通过 Registry、Router、Task、Handoff 在已注册 plow-whip Agent 间接力。",
        "locked": True,
        "scope": ["delegation"],
    },
    "R005": {
        "summary": "Update workflow state only through plow-whip state-transition commands, including submit, task, handoff, plan, review, goal, and automation; never edit AGENT_STATE.json directly.",
        "summary_zh": "工作流状态只能通过 submit、task、handoff、plan、review、goal、automation 等 plow-whip 状态迁移命令更新，禁止直接编辑 AGENT_STATE.json。",
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
        "summary": "Code-changing tasks require verification commands, every command must pass, and independent review must complete before delivery; failures remain active for repair.",
        "summary_zh": "代码修改任务必须配置验收命令并全部通过，且须完成独立 Reviewer 验收后才能交付；失败时保持 active 并继续修复。",
        "locked": False,
        "scope": ["workflow", "verification", "automation"],
    },
    "R010": {
        "summary": "Strict projects require a current scheduler-issued lease for execution and machine writeback; unleased sessions are observers.",
        "summary_zh": "严格项目只有持有 scheduler 当前租约的会话才能执行和机器回写；无租约会话只能观察。",
        "locked": True,
        "scope": ["startup", "workflow", "authorization"],
    },
}

REQUIRED_GLOBAL_RULES = {"R001", "R002", "R003", "R004", "R005", "R009", "R010"}
RULE_ENFORCEMENT = {
    "R001": "block", "R002": "block", "R003": "require_approval",
    "R004": "require_approval", "R005": "block", "R009": "verify", "R010": "block",
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
        "enforcement": {
            "mode": "strict",
            "protocol_epoch": secrets.token_hex(16),
            "control_plane": "codex",
        },
        "global_rules": {rule_id: normalize_rule(rule_id, rule, "global") for rule_id, rule in GLOBAL_RULES.items()},
        "project_rules": {},
        "agents": {
            agent: normalize_agent(agent, agent_meta.get(agent, {}))
            for agent in agents
        },
        "orchestration": dict(DEFAULT_ORCHESTRATION),
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
        schema_version = data.get("schema_version", 1)
        if not isinstance(schema_version, int):
            raise ValueError(f"invalid protocol schema version: {schema_version!r}")
        if schema_version > 4:
            raise ValueError(f"unsupported future protocol schema v{schema_version}")
        if schema_version < 4:
            data["schema_version"] = 4
            changed = True
        global_rules = data.setdefault("global_rules", {})
        for rule_id, rule in GLOBAL_RULES.items():
            canonical = normalize_rule(rule_id, rule, "global")
            current = global_rules.get(rule_id, {})
            refreshed = {**current, **canonical}
            if current != refreshed:
                global_rules[rule_id] = refreshed
                changed = True
        changed = normalize_rules(data) or changed
        changed = normalize_agents(data) or changed
        orchestration = data.setdefault("orchestration", {})
        for key, value in DEFAULT_ORCHESTRATION.items():
            if key not in orchestration:
                orchestration[key] = value
                changed = True
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


def schedulable_agents(data: dict) -> list[str]:
    return [
        name for name in enabled_agents(data)
        if data["agents"][name].get("schedulable", True)
    ]


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


def semantic_issues(data: dict) -> list[str]:
    """Report repairable schema/default drift without changing canonical files."""
    issues = []
    schema_version = data.get("schema_version")
    if isinstance(schema_version, int) and schema_version < 4:
        issues.append(f"protocol schema drifted: expected v4, got {schema_version!r}")
    elif schema_version != 4:
        issues.append(f"unsupported protocol schema: expected v4, got {schema_version!r}")
    global_rules = data.get("global_rules", {})
    drifted = []
    for rule_id, rule in GLOBAL_RULES.items():
        canonical = normalize_rule(rule_id, rule, "global")
        current = global_rules.get(rule_id)
        if current is None or any(current.get(key) != value for key, value in canonical.items()):
            drifted.append(rule_id)
    if drifted:
        issues.append("inherited global rules drifted: " + ", ".join(drifted))
    unnormalized_project_rules = [
        rule_id
        for rule_id, rule in data.get("project_rules", {}).items()
        if normalize_rule(rule_id, rule, "project") != rule
    ]
    if unnormalized_project_rules:
        issues.append("project rules need normalization: " + ", ".join(unnormalized_project_rules))
    missing_defaults = [key for key in DEFAULT_ORCHESTRATION if key not in data.get("orchestration", {})]
    if missing_defaults:
        issues.append("protocol defaults missing: orchestration." + ", orchestration.".join(missing_defaults))
    enforcement = data.get("enforcement")
    if enforcement and enforcement.get("mode") == "strict" and not enforcement.get("protocol_epoch"):
        issues.append("strict enforcement is missing protocol_epoch")
    return issues


def render_handbook(data: dict) -> str:
    orchestration = {**DEFAULT_ORCHESTRATION, **data.get("orchestration", {})}
    enforcement = data.get("enforcement") or {"mode": "legacy"}
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
    lines += [
        "",
        "## 入口与状态迁移",
        "",
        "- 人或外部工具通过 `submit` 投递任务；Agent 每次开始或恢复工作前通过 `start --agent ... --json` 获取有界上下文。",
        f"- 当前授权模式为 `{enforcement.get('mode', 'legacy')}`；严格模式下，无 scheduler 租约的会话只能获得 observer 启动包。",
        "- 系统定时任务只运行带锁的 `whip --once`；模型仅在存在可恢复的超时 active Task 时由 Worker 调用。",
        "- 机器回写必须同时通过租约、Owner、Task、Driver、dispatch、代数、签名状态有效期和 protocol epoch 校验；活跃 Worker 由 scheduler 续租，人工确认命令拒绝 Worker 租约。",
        "- Codex Desktop 人工控制权同时校验绑定线程与本机 Desktop App 进程父链；环境变量本身不构成授权。严格模式只调度可传递租约的 CLI Driver，zellij 仅保留旧模式兼容。",
        "- `AGENT_STATE.json` 通过 revision、原子写和本地 authority 签名维护；项目外 authority pin 按项目 incarnation 固定 strict 模式与 protocol epoch，归档后可复用项目名，状态或协议降级篡改都不会被加载或 repair 静默接受。",
        "- 临时嵌套子智能体受 R004 限制；Registry 中已注册 Agent 之间的 Planner、实现、Reviewer、故障接力属于 plow-whip 编排。",
        "",
        "## 无人值守闭环",
        "",
        "1. 本地分类器将明确任务直接路由；复杂、模糊或无法确认的任务交给可配置 Planner。",
        "2. Planner 只提交粗粒度里程碑；计划必须由人确认，确认后才恢复无人值守。",
        "3. 代码任务在独立 Git worktree 和任务分支执行；控制 checkout 的无租约代码改动会隔离当前任务。",
        "4. 实现完成后先提交候选 SHA；Reviewer 默认使用不同 Driver，并且验收结论绑定该准确 SHA。",
        "5. 发布进程持有推送能力；可安全自动交付时仅 fast-forward，否则只推任务分支并等待人工合并；冲突期间保留可恢复交付状态。",
        "6. scheduler 检测远端目标分支包含已验收 SHA 后自动标记 delivered，无需第二次人工确认。",
        "7. 发生二选一或必然分裂时撤销当前租约，只冻结该 Task；人工答复后签发新租约续接。",
        "8. 网络或服务故障按 Driver 独立熔断；连续探测成功达到阈值后恢复原任务与会话。",
        "",
        "## 编排默认值",
        "",
        "| 配置 | 当前值 |",
        "|---|---|",
        f"| 默认 Planner | `{orchestration['default_planner']}` |",
        f"| 默认目标分支 | `{orchestration['default_target_branch']}` |",
        f"| 系统调度间隔 | {orchestration['scheduler_interval_seconds']} 秒 |",
        f"| 每种 Driver 最大并发 | {orchestration['max_concurrency_per_driver']} |",
        f"| 实现失败重试上限 | {orchestration['retry_limit']} |",
        f"| 熔断恢复连续成功次数 | {orchestration['circuit_recovery_successes']} |",
        f"| Worker 租约时长 | {orchestration['lease_ttl_seconds']} 秒 |",
        f"| 受保护身份自动更新目标分支 | {'是' if orchestration['auto_merge_protected'] else '否，仅推任务分支'} |",
        "| 新项目无人值守 | 默认开启 |",
        "| 计划确认 | 必须由人确认 |",
        "| Git 交付 | 独立 worktree、精确 SHA Reviewer、受控发布、fast-forward only |",
        "",
        "## 文件真源",
        "",
        "| 领域 | 真源 |",
        "|---|---|",
        "| 规则、Registry、编排配置 | `collab/AGENT_PROTOCOL.json` |",
        "| 当前 Task、Workflow、Session 绑定 | `collab/AGENT_STATE.json` |",
        "| Simple-tasker 完整持久会话 | `collab/memory/sessions/<task>_simple_tasker.jsonl` |",
        "| Codex Desktop 文本镜像 | `collab/conversations/codex/current.md`（checkpoint 位于本机配置，受管环境回退到 Git 忽略的 `collab/.runtime/`） |",
        "| CLI 熔断与 Worker 进程登记 | 框架运行目录中的 `health.json`、`workers.json` |",
        "| 租约签名密钥与授权审计 | 本机配置目录的 `runtime/authority/`、`logs/authorization.jsonl`；不进入项目 |",
        "| 严格任务 worktree | 本机配置目录的 `worktrees/<project>/<task>/` |",
        "| 分支与远端交付结果 | Git refs 与远端仓库 |",
        "| 中文手册、Agent 阵容表、兼容 Markdown | 派生视图，不是真源 |",
    ]
    lines += ["", "## Agent 阵容", "", "| Agent | Roles | Driver | Schedulable | Capabilities | Assignment |", "|---|---|---|---|---|---|"]
    for agent, meta in data.get("agents", {}).items():
        if meta.get("enabled", True):
            roles = ", ".join(meta.get("roles") or [meta.get("role", agent)])
            capabilities = ", ".join(meta.get("capabilities") or []) or "—"
            schedulable = "yes" if meta.get("schedulable", True) else "no"
            lines.append(f"| `{agent}` | {roles} | {meta.get('driver', 'file')} | {schedulable} | {capabilities} | {meta.get('assignment') or '—'} |")
    if "goal-planner" in data.get("agents", {}):
        lines += ["", "> `goal-planner` 仅保留兼容；默认规划使用 `orchestration.default_planner`，除非任务明确指定。"]
    if "simple-tasker" not in data.get("agents", {}):
        lines += ["", "> `simple-tasker` 是内置按需 Agent：首次命中简单任务路由时自动注册；未注册不表示 Driver 不受支持。"]
    lines += [
        "",
        "> `file` Driver 只负责 inbox/人工接管，不构成端到端无人值守；自动 Reviewer 会选择可执行 CLI Driver。",
        "",
        "## 密钥与网络边界",
        "",
        "- Codex/Cursor 可使用 Desktop 登录或只保存环境变量名称的 Key Pool；真实 Key 不写入项目、状态或日志。",
        "- Codex Desktop 与默认 Cursor Desktop 都是 `schedulable=false` 的控制面和人工入口；只能提交、查看、确认或答复决策，不拥有 Task。",
        "- 严格项目的控制命令必须由当前绑定的 Codex Desktop thread 授权；Worker 不继承 Desktop origin/thread，清除租约变量也不能变成人工控制面。切换控制会话必须显式执行 Desktop 同步。",
        "- Desktop 同步仅在 `CODEX_INTERNAL_ORIGINATOR_OVERRIDE=Codex Desktop` 时注册 `CODEX_THREAD_ID`，仅保存 user 与 assistant commentary/final_answer（兼容 final）文本；system、developer、reasoning、tool 与其他内容不会写入项目，公开状态只记录不可逆 thread_ref。",
        "- DeepSeek Key 只从 `DEEPSEEK_API_KEY` 或编号环境变量读取；仅记录后四位与哈希组成的脱敏标识。",
        "- Simple-tasker 在项目沙箱内读写、测试并持久化本地 JSONL Session；禁止自行提交、推送、合并或越出项目。",
        "- 国内网络、海外出口、TLS 与 Provider 分开探测；全局海外网络故障暂停外部 CLI，单 Provider 故障只暂停对应 Driver。",
    ]
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

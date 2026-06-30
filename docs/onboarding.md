# Onboarding Guide / 接入指南

## Quick Start for New Projects

### 1. Install

```bash
pip install plow-whip
```

### 2. Configure

```bash
plow-whip configure --projects-dir ~/my-projects --agents qoder codex cursor
```

This creates `~/.plow-whip/config.json` with your settings.

### 3. Initialize Project

```bash
plow-whip init --project MyProject
```

This generates:
```
MyProject/
└── collab/
    ├── CONVENTIONS.md          ← Rules (from template)
    ├── AGENT_STATE.json        ← State machine state
    ├── AGENT_COMMS.md          ← Message board
    ├── conversations/          ← Per-agent sessions
    │   ├── qoder/current.md
    │   ├── codex/current.md
    │   └── cursor/current.md
    └── memory/                 ← Multi-layer memory
        ├── PROJECT.md
        ├── CURRENT_STATUS.md
        ├── NEXT_ACTION.md
        ├── ROADMAP.md
        ├── DECISIONS.md
        ├── adr/
        ├── sessions/
        └── sprints/
```

### 4. Start Working

```bash
# Check who's turn it is
plow-whip --project MyProject status

# Handoff to next agent
plow-whip --project MyProject handoff \
  --output "Implemented login API" \
  --next "Review architecture and write tests" \
  --phase "Sprint-001"

# Rotate a session that's getting too long
plow-whip --project MyProject rotate --agent qoder \
  --topic "Phase 2 Review" --summary "Architecture accepted"
```

### 5. Framework Updates

When you update plow-whip (new version with improved templates):

```bash
pip install --upgrade plow-whip
plow-whip sync
```

This pushes updated templates to all your projects.

---

## 中文

### 快速开始

1. `pip install plow-whip`
2. `plow-whip configure --projects-dir ~/my-projects`
3. `plow-whip init --project MyProject`
4. 开始使用：`plow-whip --project MyProject status`
5. 框架更新后：`plow-whip sync`

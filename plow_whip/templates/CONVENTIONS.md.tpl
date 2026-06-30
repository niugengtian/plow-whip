# AI Collaboration Conventions — {PROJECT_NAME}

> Read this at session start. References other files for details.

---

## 1. Single-Direction Responsibility

| Role | Allowed | Forbidden |
|------|---------|-----------|
| **Human** | Final decisions, acceptance | — |
| **Codex** | Sole Code Owner, commits code | Change requirements/product direction |
| **Qoder** | PM + Architect: requirements, Sprint planning, architecture review, test acceptance | — |
| **Cursor** | Bug reports, fix suggestions | Change product design |

Human has final decision authority. All AI suggestions need human confirmation.

---

## 2. State Machine Handoff

```bash
# Check current turn
plow-whip --project {PROJECT_NAME} status

# Handoff
plow-whip --project {PROJECT_NAME} handoff \
  --output "what was done" --next "what's next" --phase "phase name" \
  --files file1 file2 --verify "verify command"
```

Rule: always handoff after work, carry full context.

---

## 3. File Deletion

**No `rm`.** Use `mv` to `by_rm/`, naming: `original_name_YYYYMMDD_HHMMSS.ext`

```bash
mkdir -p by_rm/<dir> && mv <file> by_rm/<dir>/<name>_YYYYMMDD_HHMMSS.<ext>
```

`by_rm/` is not tracked by git.

---

## 4. Session Entry Order

1. Hot Layer (<30s): `{PROJECT_NAME}/collab/memory/PROJECT.md` + `CURRENT_STATUS.md` + `NEXT_ACTION.md`
2. This file (learn the rules)
3. `{PROJECT_NAME}/collab/AGENT_COMMS.md` (check for messages to yourself)
4. State machine turn (run status)
5. On demand: ROADMAP / Sprint / ADR / DECISIONS

---

## 5. Code Standards

- Files ≤200 lines, functions ≤30 lines, split when needed
- `snake_case` for files/functions, `UPPER_SNAKE_CASE` for constants

---

## 6. Conversation Rotation

Each AI owns an independent session directory `{PROJECT_NAME}/collab/conversations/<agent>/`.

**Commands:**
```bash
# Check session status
plow-whip --project {PROJECT_NAME} sessions-overview
plow-whip --project {PROJECT_NAME} session --agent qoder

# Rotate (archive current.md + create fresh template)
plow-whip --project {PROJECT_NAME} rotate --agent qoder --topic "topic" --summary "summary"
```

**Rules:**
- `current.md` exceeds 100 lines or 8KB → must rotate
- Check rotation need at every handoff
- Archived files contain summary + original content, traceable
- New session reads `current.md` to restore context

---

## 7. File Index

**Project Memory:** `{PROJECT_NAME}/collab/memory/` → PROJECT / STATUS / NEXT / ROADMAP / DECISIONS / CHANGELOG / sessions / sprints / adr

**Live Collaboration:** `{PROJECT_NAME}/collab/` → AGENT_STATE / AGENT_COMMS / CONVENTIONS / conversations

---

*Last updated: {DATE}*

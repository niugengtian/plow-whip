# Agents — plow-whip

| Agent | Roles | Driver | Capabilities | Assignment |
|---|---|---|---|---|
| `codex` | planner, coordinator, reviewer | codex_cli | * | — |
| `cursor` | implementation, reviewer | zellij | * | 桌面打工仔 |
| `cursor_cli` | planner, implementation, reviewer | cursor_cli | * | CLI 打工仔 |
| `codex_cli` | planner, implementation, reviewer | codex_cli | * | — |
| `reviewer` | reviewer | file | review | Review implementation and risks |
| `goal-planner` | planner | codex_cli | e2e-plan | Plan coarse goals |
| `e2e-worker` | e2e-worker | cursor_cli | e2e-readonly | Run read-only implementation acceptance |
| `e2e-worker-backup` | e2e-worker | codex_cli | e2e-readonly | Take over failed E2E worker runs |
| `e2e-auditor` | e2e-auditor | codex_cli | e2e-review | Independently accept completed E2E work |

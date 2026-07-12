#!/bin/bash
# 兼容旧脚本：转调 plow-whip drive
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PROJECT="${PLOW_WHIP_PROJECT:-plow-whip}"
TASK="${1:-完成 AGENT_STATE.json 中的 next_action}"

cd "$ROOT"
exec python3 -m plow_whip.agent_flow --project "$PROJECT" drive cursor_cli \
  --next "$TASK" --from-agent cursor

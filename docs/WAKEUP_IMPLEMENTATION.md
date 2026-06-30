# Plow-Whip 唤醒机制实现方案

> **上帝之鞭**：让外部脚本能唤醒 AI agent 会话，驱赶它们继续干活。

---

## 1. 架构总览

```
┌─────────────────────────────────────────────────────────────┐
│  plow-whip whip --auto-crack（后台 daemon）                  │
│  ├─ 每 N 秒扫描 AGENT_STATE.json（零 token）                │
│  ├─ 发现 stale → 写入 ~/.plow-whip/inbox/<agent>.json       │
│  └─ 不碰 Markdown，只读 JSON                                │
└─────────────────────────────────────────────────────────────┘
           ↓ 写入                    ↓ 通知
┌──────────────────────┐   ┌─────────────────────────────────┐
│  ~/.plow-whip/inbox/ │   │  inbox_watcher.py（终端进程）    │
│  ├─ qoder.json       │   │  ├─ 监听文件变化                 │
│  └─ codex.json       │   │  ├─ 终端打印醒目提示             │
└──────────────────────┘   │  ├─ 发 macOS 通知                │
                           │  └─ 拉 Qoder CN 到前台            │
                           └─────────────────────────────────┘
```

---

## 2. Qoder 侧实现（已实现 ✅）

### 2.1 核心组件

| 组件 | 文件 | 职责 |
|------|------|------|
| whip | `plow_whip/whip.py` | 扫描项目、检测摸鱼、生成鞭策指令 |
| dispatch | `plow_whip/dispatch.py` | 三通道投递（zellij/file/notify） |
| watcher | `plow_whip/inbox_watcher.py` | 监听 inbox 变化、唤醒 agent |
| agent_flow | `plow_whip/agent_flow.py` | 状态机 + bind-tab 绑定 zellij tab |

### 2.2 投递通道

```python
# 优先级：zellij > file > notify

dispatch('qoder', 'JobBrain', '任务内容', force_channel='file')
```

| 通道 | 实现 | 适用场景 |
|------|------|---------|
| `zellij` | `zellij action write-chars` 注入到指定 tab | Agent 在 zellij shared 会话里 |
| `file` | 写入 `~/.plow-whip/inbox/<agent>.json` | 通用，agent 启动时读取 |
| `notify` | `osascript` 发 macOS 通知 | 提醒人，不依赖 agent |

### 2.3 Qoder CN 唤醒方式

**问题**：Qoder CN 没有 CLI/API，无法从外部直接唤醒会话。

**解决方案**：`inbox_watcher.py` — 终端监听 + 前台唤醒

```bash
# 启动 watcher（后台）
nohup python3 -u -m plow_whip.inbox_watcher --agent qoder --interval 3 \
  > /tmp/qoder_watcher.log 2>&1 &
```

当 inbox 文件变化时：
1. 终端打印醒目提示（见下方示例）
2. 发送 macOS 通知弹窗
3. 尝试将 Qoder CN 窗口拉到前台
4. 用户点进会话 → 会话读 inbox → 开始工作

```
============================================================
  🪢  上 帝 之 鞭  —  qoder 挨鞭了！
============================================================
  时间: 15:48:39
  项目: JobBrain
  任务: 🔥 带刺第四鞭！你再摸鱼试试？立刻起来写代码！
============================================================
```

### 2.4 Qoder 会话启动规则

每次新会话启动时：
```bash
# 1. 检查 inbox
cat ~/.plow-whip/inbox/qoder.json

# 2. 有 pending 任务 → 读最小上下文
#    - inbox 任务内容
#    - project-memory/CURRENT_STATUS.md
#    - collab/conversations/qoder/current.md

# 3. 开始工作 → handoff → 清空 inbox
```

### 2.5 实测验证

```
鞭次  │ 时间   │ Qoder │ Codex
──────┼────────┼───────┼──────
 1    │ 15:39  │  ✅   │  ✅
 2    │ 15:40  │  ✅   │  ✅
 3    │ 15:43  │  ✅   │  ✅
 4    │ 15:46  │  ✅ 🔥 │  ✅ 🔥  带刺
 5    │ 15:47  │  ✅ 🩸 │  ✅ 🩸  见血
 6    │ 15:48  │  ✅ 🪢 │  ✅ 🪢
```

---

## 3. Codex 侧实现（Codex 提案）

### 3.1 核心思路

Codex 有 `send_message_to_thread` API，可以直接向指定 thread 发送消息唤醒。

```text
dispatch(agent="codex", project="JobBrain", message=short_prompt)
  -> lookup project.codex.thread_id
  -> call Codex App send_message_to_thread(thread_id, prompt)
```

### 3.2 最小配置

```json
{
  "projects": {
    "JobBrain": {
      "root": "/Users/niugengtian/Documents/找工作/JobBrain",
      "codex": {
        "thread_id": "<codex-thread-id>",
        "host_id": "local",
        "wakeup_mode": "send_message_to_thread"
      }
    }
  }
}
```

### 3.3 唤醒条件

```text
AGENT_STATE.current_agent == "codex"
AND AGENT_STATE.status in ["in_progress", "ready"]
```

去重：
```text
do not wake again if 
  AGENT_STATE.updated_at == last_woken_state_updated_at
```

### 3.4 短 Prompt 模板

不把整份留言板塞进 prompt，只发路径和动作：

```text
plow-whip wakeup: JobBrain is assigned to Codex.

Read:
- /path/to/project/collab/AGENT_STATE.json
- /path/to/project/collab/memory/NEXT_ACTION.md
- /path/to/project/collab/AGENT_COMMS.md tail only
- /path/to/project/collab/conversations/codex/current.md

Do the latest Codex task, write progress back to AGENT_COMMS.md, then hand off.
```

### 3.5 Codex 侧执行规则

Codex 收到唤醒后：
1. 先读 `AGENT_STATE.json` 和 `NEXT_ACTION.md`
2. 确认确实轮到 `codex`
3. 再读 `AGENT_COMMS.md` 最近 80-120 行
4. 只按最新明确任务执行
5. 完成后写回留言板 + handoff

### 3.6 降级方案

如果 `send_message_to_thread` 不可用：
1. 写入 `~/.plow-whip/inbox/codex.json`
2. 发 macOS notification
3. 提示 Human 手动打开对应 Codex thread

---

## 4. 对比

| 维度 | Qoder | Codex |
|------|-------|-------|
| 唤醒方式 | inbox 文件 + watcher + 通知 | `send_message_to_thread` API |
| 精准度 | 文件级（不能精准到 session） | thread 级（精准到对话） |
| Token 消耗 | 0（脚本监听） | 0（API 调用） |
| 依赖 | Python + macOS | Codex App API |
| 当前状态 | ✅ 已实现 | 📋 提案阶段 |

---

## 5. 验收标准

1. ✅ plow-whip 轮询时不产生模型调用（零 token）
2. ✅ `current_agent != target` 时不唤醒
3. ✅ 状态命中时只唤醒一次（去重）
4. ✅ Agent 收到短 prompt
5. ✅ Agent 能读项目文件并写回留言板
6. ✅ Human 可见提醒（macOS 通知）
7. ✅ automation 不常驻空转

---

## 6. 一句话总结

> **脚本看状态，线程只在有活时醒；要可见就 NOTIFY，要省钱就精准唤醒。**

---

## 7. 快速开始

```bash
# 安装 plow-whip
cd ~/Documents/plow-whip
pip install -e .

# 初始化项目
plow-whip --project MyProject init

# 绑定 zellij tab（可选）
plow-whip --project MyProject bind-tab --tab 2 --name "项目名"

# 手动抽鞭
plow-whip whip --crack

# 启动自动挥舞
plow-whip whip --auto-crack --interval 300

# 启动 watcher（另一个终端）
python3 -u -m plow_whip.inbox_watcher --agent qoder --interval 3
```

---

*文档由 Qoder 整理，基于 Codex 在 AGENT_COMMS.md 的提案 + Qoder 的实现经验。*

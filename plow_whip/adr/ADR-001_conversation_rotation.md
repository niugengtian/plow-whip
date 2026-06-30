# ADR-001: AI 会话目录与轮转机制

**状态：** ✅ Accepted（Qoder + Codex 共识，待 Human 最终确认）
**日期：** 2026-06-30
**提出者：** Human + Qoder

---

## 背景

多 AI 协作中，每次对话的上下文窗口不断膨胀：
- 历史聊天记录占据大量 token
- 大部分历史内容已过时，但 AI 无法主动丢弃
- 上下文越大 → 响应越慢、成本越高、质量越差

当前 `AGENT_COMMS.md` 是共享留言板，没有按 AI 隔离的会话历史。

## 决策

为每个 AI 工具建立独立的会话目录，采用类似 log rotation 的机制管理对话文件。

### 目录结构

```
agent_flows/
├── conversations/
│   ├── qoder/
│   │   ├── current.md                  # 当前活跃会话（保持精简）
│   │   ├── 20260630_030000_phase1_review.md    # 归档
│   │   └── 20260630_040000_sprint001_plan.md   # 归档
│   ├── codex/
│   │   ├── current.md
│   │   └── 20260630_020000_phase2_impl.md
│   └── cursor/
│       ├── current.md
│       └── ...
```

### 切割策略

| 触发条件 | 动作 |
|---------|------|
| `current.md` 超过 **20 条消息** 或 **8KB** | 归档为 `YYYYMMDD_HHMMSS_<topic>.md`，新建空 `current.md` |
| 每次 **handoff** 完成 | 自动触发切割 |
| 每个 **Sprint 结束** | 强制切割 + 写 Session Summary |

### 归档文件内容

归档文件**不是原始聊天记录的复制**，而是**结构化摘要**：

```markdown
# Session: Phase 2 Architecture Review
**AI:** Qoder
**时间：** 2026-06-30 03:00 - 03:45
**话题：** Phase 2 架构审查

## 关键决策
- D-010: Schema Accepted，优化项后续处理
- ...

## 产出物
- DECISIONS.md 新增 D-010
- NEXT_ACTION.md 更新 Sprint-001 任务

## 未完成/遗留
- ...
```

### 上下文恢复链路（三层接力）

```
新会话启动
    ↓
1. Hot Layer: project-memory/ (PROJECT + STATUS + NEXT_ACTION)
   → 30秒内恢复项目状态
    ↓
2. Warm Layer: conversations/<me>/current.md + Session Summaries
   → 恢复近期工作上下文和待办
    ↓
3. Cold Layer: conversations/<me>/归档文件 (按需)
   → 回溯历史细节
```

## 待讨论问题（Codex 已反馈，Qoder 整合）

| # | 问题 | Codex 意见 | Qoder 结论 |
|---|------|----------|----------|
| 1 | 切割阈值 | 20条/8KB 合理，可先用 | ✅ 同意，先执行再调优 |
| 2 | 摘要质量 | AI 摘要有遗漏风险，建议保留原始文件路径或原文副本 | ✅ 归档文件包含摘要 + 原始文件路径引用 |
| 3 | 实现方式 | 切割逻辑放 `agent_flow.py` 自动执行 | ✅ 同意，自动化优于人工 |
| 4 | current.md 格式 | 应固定模板 | ✅ 模板：上次遗留 / 当前任务 / 关键决策 |
| 5 | 与现有机制关系 | 与 `AGENT_COMMS.md` 共存：留言板做对讲机，会话目录做个人上下文 | ✅ 同意，职责分离清晰 |

## 预期收益

- 每次新对话上下文精简 → 响应更快、成本更低
- 每个 AI 只看自己的会话 → 减少无关信息干扰
- 结构化归档 → 可追溯、可检索
- 与 Project Memory 三层架构对齐 → 不增加认知负担

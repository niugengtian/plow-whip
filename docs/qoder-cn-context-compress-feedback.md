# Qoder CN 会话上下文管理功能反馈

## 用户问题

作为重度用户，发现一个严重影响使用体验的问题：

**会话上下文只增不减，无法释放内存**

- 当前会话上下文已达 80KB+
- 随着对话增长，响应变慢、token 消耗巨大
- 没有官方方式压缩或释放当前会话的内存上下文

## 核心诉求

需要以下 API 让开发者可以用代码管理会话上下文：

### 1. 上下文压缩 API（最紧急）

```typescript
// 压缩当前会话上下文，保留摘要，丢弃细节
qoder.session.compress(options?: {
  keepRecentMessages?: number;  // 保留最近 N 条消息
  summaryStyle?: 'brief' | 'detailed';  // 摘要详细程度
}): Promise<{
  originalSize: number;
  compressedSize: number;
  summary: string;
}>
```

**使用场景**：
- 长对话后主动压缩，释放内存
- 定时任务自动压缩超阈值会话
- 保持响应速度，降低 token 消耗

### 2. 上下文信息查询 API

```typescript
// 获取当前会话的上下文信息
qoder.session.getContextInfo(): Promise<{
  sizeBytes: number;        // 上下文大小
  messageCount: number;     // 消息数量
  tokenCount: number;       // Token 数量
  lastCompressed: Date;     // 上次压缩时间
}>
```

**使用场景**：
- 监控上下文增长
- 触发自动压缩的条件判断
- 显示在状态栏让用户感知

### 3. 自动压缩机制

```typescript
// 配置自动压缩
qoder.session.setAutoCompress(config: {
  enabled: boolean;
  thresholdKB: number;      // 超过此大小自动压缩
  thresholdMessages: number; // 超过此消息数自动压缩
  keepRecent: number;       // 压缩后保留最近 N 条
}): Promise<void>
```

**使用场景**：
- 无需手动干预，自动保持上下文精简
- 类似浏览器的标签页休眠机制

## 当前 workaround 的局限性

目前只能通过手动开新会话来"释放"上下文：
1. 新建对话 → 从截断后的 JSONL 加载
2. 但丢失了当前对话的完整上下文
3. 需要重新建立对话，体验割裂

**理想方案**：在当前会话内压缩，保留关键上下文，释放内存。

## 技术实现建议

参考业界做法：
- **Claude**：自动总结长对话，保留关键信息
- **ChatGPT**：Memory 机制，跨会话保留关键记忆
- **Cursor**：@codebase 索引，按需检索相关代码

Qoder CN 可以结合：
1. 本地 LLM 生成会话摘要
2. 向量数据库存储历史记忆
3. RAG 机制按需检索相关上下文

## 用户价值

1. **性能提升**：减少上下文大小 → 响应更快
2. **成本降低**：减少 token 消耗 → 更省钱
3. **体验优化**：长对话不卡顿 → 更流畅
4. **开发者友好**：API 可编程 → 可自动化

## 期望优先级

1. 🔴 **P0**：`qoder.session.compress()` — 手动压缩 API
2. 🟡 **P1**：`qoder.session.getContextInfo()` — 上下文信息查询
3. 🟢 **P2**：自动压缩机制 — 无需手动干预

---

**用户信息**：
- 使用场景：多 Agent 协作开发（plow-whip 项目）
- 会话时长：单次会话 2-4 小时
- 上下文峰值：80KB+（经常遇到）

**相关项目**：
- plow-whip：https://github.com/niugengtian/plow-whip
- 已实现基于文件的会话轮转，但无法解决内存上下文问题

希望 Qoder CN 越来越好！

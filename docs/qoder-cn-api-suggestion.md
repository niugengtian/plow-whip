# Qoder CN 会话操作 API 建议

## 背景

Qoder CN 是一款优秀的 AI 桌面 IDE，但会话历史管理目前只能通过手动操作底层 JSONL 文件实现。
随着对话增长，上下文膨胀会导致性能下降，开发者需要自动化手段来管理会话生命周期。

## 当前痛点

1. **会话文件无限增长** - 长对话导致 JSONL 文件膨胀，上下文压缩不够积极
2. **无法程序化管理** - 开发者只能手动删除或归档，无法集成到自动化流程
3. **历史检索困难** - 没有官方 API 搜索历史会话内容
4. **多项目切换** - 无法快速获取某个项目的所有会话摘要

## 建议的 API 设计

### 1. 会话信息查询 API

```typescript
// 获取当前会话信息
interface SessionInfo {
  id: string;           // 会话 ID
  projectId: string;    // 项目 ID
  createdAt: Date;      // 创建时间
  messageCount: number; // 消息数量
  tokenCount: number;   // Token 数量
  sizeBytes: number;    // 文件大小
  isActive: boolean;    // 是否活跃
}

// API
qoder.session.getCurrent(): Promise<SessionInfo>
qoder.session.list(projectId?: string): Promise<SessionInfo[]>
qoder.session.get(sessionId: string): Promise<SessionInfo | null>
```

### 2. 会话轮转/归档 API

```typescript
// 轮转配置
interface RotationConfig {
  maxLines: number;        // 最大行数阈值
  maxSizeKB: number;       // 最大文件大小阈值
  keepRecentLines: number; // 保留最近行数
}

// API
qoder.session.rotate(sessionId: string, config?: RotationConfig): Promise<{
  archivedPath: string;    // 归档文件路径
  remainingLines: number;  // 剩余行数
}>
qoder.session.archive(sessionId: string, targetPath: string): Promise<void>
```

### 3. 会话搜索 API

```typescript
// 搜索结果
interface SearchResult {
  sessionId: string;
  messageId: string;
  snippet: string;         // 匹配片段
  timestamp: Date;
  role: 'user' | 'assistant';
}

// API
qoder.session.search(query: string, options?: {
  projectId?: string;
  dateRange?: [Date, Date];
  role?: 'user' | 'assistant';
  limit?: number;
}): Promise<SearchResult[]>
```

### 4. 会话导出/导入 API

```typescript
// 导出格式
interface ExportOptions {
  format: 'jsonl' | 'markdown' | 'html';
  includeMetadata: boolean;
  dateRange?: [Date, Date];
}

// API
qoder.session.export(sessionId: string, options: ExportOptions): Promise<Blob>
qoder.session.import(data: Blob, format: string): Promise<SessionInfo>
```

### 5. 事件订阅 API

```typescript
// 事件类型
type SessionEvent = 
  | { type: 'message-added'; sessionId: string; messageCount: number }
  | { type: 'session-rotated'; sessionId: string; archivedPath: string }
  | { type: 'session-closed'; sessionId: string };

// API
qoder.session.on(event: string, callback: (event: SessionEvent) => void): Disposable
```

## 使用场景

### 场景 1：自动化会话轮转

```typescript
// 定时任务：每 30 分钟检查并轮转超阈值会话
const config = { maxLines: 100, maxSizeKB: 20, keepRecentLines: 50 };

for (const session of await qoder.session.list()) {
  const info = await qoder.session.get(session.id);
  if (info.messageCount > config.maxLines || info.sizeBytes > config.maxSizeKB * 1024) {
    await qoder.session.rotate(session.id, config);
    console.log(`Rotated session ${session.id}`);
  }
}
```

### 场景 2：项目会话摘要生成

```typescript
// 为当前项目生成会话摘要报告
const projectSessions = await qoder.session.list(currentProjectId);
const summaries = [];

for (const session of projectSessions) {
  const results = await qoder.session.search('', { 
    projectId: currentProjectId,
    limit: 5 
  });
  summaries.push({
    sessionId: session.id,
    date: session.createdAt,
    firstQuery: results[0]?.snippet || 'No messages'
  });
}

// 输出 Markdown 报告
console.log(generateMarkdownReport(summaries));
```

### 场景 3：跨会话知识检索

```typescript
// 搜索所有历史会话中关于 "认证" 的讨论
const results = await qoder.session.search('认证 JWT token', {
  limit: 20,
  role: 'assistant'  // 只搜索 AI 回复
});

// 汇总相关知识
const knowledge = results.map(r => ({
  session: r.sessionId,
  context: r.snippet,
  time: r.timestamp
}));
```

## 收益

1. **开发者体验** - 桌面开发者可以用代码管理会话，无需手动操作文件
2. **自动化集成** - 可集成到 CI/CD 流程或定时任务
3. **知识复用** - 跨会话搜索让历史经验可检索
4. **生态扩展** - 第三方工具可以基于 API 构建更高级的会话管理功能

## 参考实现

耕田之鞭 (plow-whip) 项目已实现了一个基于文件操作的会话管理器：
- 仓库：https://github.com/niugengtian/plow-whip
- 模块：`plow_whip/qoder_session.py`

该实现通过直接操作 JSONL 文件实现了类似功能，但如果有官方 API，
代码会更简洁、更可靠，也能支持更多高级场景。

---

*此建议由耕田之鞭项目团队提出，希望 Qoder CN 越来越好！*

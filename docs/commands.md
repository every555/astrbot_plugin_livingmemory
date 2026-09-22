# 命令速查

LivingMemory 的命令统一使用 `/lmem` 前缀。

| 命令 | 说明 |
| --- | --- |
| `/lmem status` | 查看记忆库状态 |
| `/lmem search <query> [k]` | 搜索长期记忆，`k` 默认为 5 |
| `/lmem forget <id>` | 删除指定记忆 |
| `/lmem rebuild-index` | 重建文档索引 |
| `/lmem rebuild-graph` | 重建图谱记忆索引 |
| `/lmem webui` | 查看 WebUI 入口信息 |
| `/lmem summarize` | 立即总结当前会话 |
| `/lmem reset` | 重置当前会话记忆上下文 |
| `/lmem cleanup [preview\|exec]` | 清理历史消息中的旧记忆注入片段 |
| `/lmem help` | 显示帮助 |
| `/lmem trace` | 查看最近一次 Context 组装的完整 trace（v5.4 新增） |

## v5.x 新增功能（春雪维护版）

### v5.2 — 四大核心升级

| 功能 | 说明 |
| --- | --- |
| 核心记忆索引 | 始终在线的高优先级记忆索引，关键信息每次对话都可访问 |
| 图增强召回 | 利用图谱关系增强跨记忆关联召回 |
| 决策追踪 | 记录 LLM 关键决策路径，便于回溯 |
| 注入链重排 | 对注入上下文的记忆片段智能重排序 |

### v5.3 — 会话摘要系统

| 功能 | 说明 |
| --- | --- |
| 空闲自动摘要 | 会话空闲超过 30 分钟自动生成摘要 |
| 摘要衰减策略 | 旧摘要按时间衰减，避免过度堆积 |

### v5.4 — Context 组装显式化

| 功能 | 说明 |
| --- | --- |
| AssemblyTrace | 记录每次上下文组装的完整链路 |
| ContextTraceStore | 独立 SQLite 存储 trace，不干扰主库 |
| 情感路由 | 根据用户情绪自动调整召回策略 |
| 活跃窗口加成 | 近期记忆获得时间衰减加成 |
| 自省注入器 | 每次注入后生成自然语言摘要 |

## 常用排查

| 现象 | 建议 |
| --- | --- |
| 搜不到刚聊过的内容 | 先执行 `/lmem summarize`，确认对话已经写入长期记忆 |
| 记忆明显串到其他人格 | 检查 `filtering_settings.use_persona_filtering` 是否开启 |
| 群聊上下文不完整 | 检查 `session_manager.enable_full_group_capture` 是否开启 |
| 索引疑似异常 | 执行 `/lmem rebuild-index`，图谱异常则执行 `/lmem rebuild-graph` |

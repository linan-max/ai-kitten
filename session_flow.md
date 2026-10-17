# NexusCompanion 业务流程文档

## 1. 新建会话流程

1. 用户注册 / 登录，获得 JWT（`Authorization: Bearer <token>`）。
2. 用户选择已有角色（或通过 `POST /agents` 新建角色卡）。
3. 弹出新建会话窗口：
   - 单选：**人设锁会话 (cakumi)** / **长篇叙事会话 (mufy)**
   - 若选 mufy，附加复选框「继承角色静态人设」，默认勾选。
4. `POST /sessions` 创建记录，写入 `session_type`；**创建后类型不可修改**。

## 2. Cakumi 人设锁会话执行流程

1. 前端 `POST /chat`（`stream=true` 时走 SSE），后端识别 `session_type = cakumi`。
2. 用户消息先落库 `messages`（LLM 失败也不丢用户输入）。
3. 读取该角色全局 `static_memory`（静态显式人设）。
4. 调用 Mem0 检索该 `user_id + agent_id` 下的动态 RAG 记忆（未安装/未配置时降级为占位文本）。
5. 按 `build_cakumi_messages` 组装 messages（system 人设 → 窗口化历史 → 用户输入），调用 LLM，流式返回。
6. 流结束后保存 assistant 消息到 `messages` 表。
7. 后台异步：将本轮对话交给 Mem0 自动抽取人物细碎记忆。
8. **不访问 `story_timeline`，不生成剧情事件**。
9. 前端展示【记忆管理面板】，隐藏时间线面板。

## 3. 长篇叙事会话执行流程

1. 前端 `POST /chat`，后端识别 `session_type = mufy`。
2. 用户消息先落库。
3. 根据 `inherit_static_memory` 开关，决定是否把静态人设塞进 system。
4. 读取当前 `session_id` 对应的 `story_timeline` 时序事件（按 id 排序）。
5. 按 `build_mufy_messages` 组装 messages，调用 LLM，流式返回。
6. 流结束后保存 assistant 消息。
7. 后台异步：调用 LLM 执行 `EXTRACT_STORY_EVENT_PROMPT`，解析 JSON，`need_save=true` 时插入 `story_timeline`（`is_manual=0`）；
   - 若 `NEXUS_CONFLICT_CHECK_ENABLED=true` 且继承静态人设，先跑 `CONFLICT_CHECK_PROMPT`，冲突则丢弃该事件。
8. **不调用 Mem0，不抽取人物细碎记忆**。
9. 前端展示【故事时间线面板】，隐藏动态记忆编辑面板。

## 4. 长对话管理（两模式通用）

1. 进入 Prompt 的历史 = 最近 `NEXUS_RECENT_KEEP`（默认 16）条原文。
2. 每轮聊天后，后台检查 `messages` 总数：
   - 超过 `NEXUS_SUMMARY_THRESHOLD`（默认 30）且摘要未覆盖到当前窗口边界时，
   - 把「窗口之外的旧消息」交给 LLM 做**增量滚动摘要**（与旧摘要融合），写入 `session_summary.covered_until_msg_id`。
3. 下次聊天时，摘要作为 system 的一部分进入上下文，兼顾长期记忆与 Token 成本。

## 5. 数据隔离规则

| 数据 | 绑定键 | 可见会话 |
|---|---|---|
| `static_memory` 静态人设 | `agent_id` | 该角色所有会话共享 |
| Mem0 动态记忆 | `user_id + agent_id` | 仅 cakumi 会话使用 |
| `story_timeline` 剧情事件 | `session_id` | 仅 mufy 会话使用 |
| `session_summary` 滚动摘要 | `session_id` | 仅当前会话 |
| `user_llm_config` 用户 Key | `user_id` | 该用户全部请求 |
| `messages` 消息记录 | `session_id` | 仅当前会话 |

## 6. Prompt 组装优先级

- Cakumi：静态人设 > RAG 记忆 > 历史对话
- 长篇叙事：静态人设（可选）> 时序剧情事件 > 历史对话

> 静态人设永远是最高优先级，保证不会被剧情带偏。
> 结构上由 messages 数组保证：system（人设/记忆/时间线）恒在最前，历史按时间排列，用户输入在末尾。

## 7. 后续扩展点

- 剧情分支：`story_timeline.branch_id` 已预留，前端可按 `causal_parent_id` 渲染事件树，从任意事件分叉出新故事线。
- 存档读档：把 `messages + story_timeline + static_memory` 打包成 JSON 导出 / 导入。
- 角色卡导入导出：`agents` 表字段 + 静态记忆打包。
- 多模型切换：`user_llm_config` 已支持任意 OpenAI 兼容端点，后续可加按会话选模型。

# NexusCompanion Code Wiki

> 面向开发者的结构化代码导航文档。覆盖：整体架构、模块职责、关键类与函数、依赖关系、数据模型与运行方式。
> 版本基线：v2.1（融合 HerAndHim / AstrBot / mem0 / Mufy 设计思路）。

---

## 1. 项目概述

**NexusCompanion** 是一个自研的 AI 伴侣 Web 应用后端，核心卖点是 **双模式会话隔离**：

| 会话模式 | `session_type` | 记忆方式 | 特点 |
|---|---|---|---|
| **Cakumi 人设锁会话** | `cakumi` | 静态显式人设（`static_memory`）+ Mem0 向量 RAG 动态记忆（`memory.py`） | 锁人设、防 OOC，不生成剧情时间线 |
| **Mufy 长篇叙事会话** | `mufy` | 时序剧情事件时间线（`story_timeline`）+ 可选继承静态人设 | 支持事件因果、剧情分支、冲突检测，不使用 Mem0 |

> 两种模式数据完全隔离：同一角色可并行创建多条不同类型会话互不干扰；`session_type` 创建后不可修改。

仓库中除根目录的自研代码外，还包含 5 个带 `.git` 的开源参考项目克隆（`AstrBot` / `GirlfriendGPT` / `HerAndHim` / `letta` / `mem0`），仅作功能借鉴来源，**运行时不会被主程序 import**（Mem0 走的是 pip 安装的 `mem0ai` 包）；具体引用说明见 **§7.3**。

---

## 2. 技术栈

- **后端**：Python 3.10+ / FastAPI（异步 + `run_in_threadpool` 包装同步 SQLite）
- **数据库**：SQLite（WAL 模式，每操作独立连接），schema 由 `schema.sql` 启动时幂等执行
- **向量记忆**：Chroma（本地文件）→ 经 Mem0 封装（可选依赖，缺失自动降级）
- **LLM 网关**：OpenAI 兼容协议（DeepSeek / Ollama / vLLM / OneAPI 等），支持每用户独立 API Key + SSE 流式
- **认证**：bcrypt（sha256 预哈希）密码 + JWT（python-jose, HS256）
- **流式通信**：SSE（`text/event-stream`）
- **前端**（README 声明，本仓库仅后端）：Vue3 + Element Plus + Pinia

---

## 3. 整体架构

```
┌─────────────────────────────────────────────────────────────────┐
│                         HTTP 客户端（前端）                       │
└───────────────┬─────────────────────────────────────────────────┘
                │ REST + SSE
┌───────────────▼─────────────────────────────────────────────────┐
│                      main.py  (FastAPI 应用)                     │
│   lifespan 启动: db.init_db() + Mem0 可用性探测                  │
│                                                                 │
│  ┌─ 认证层 ───────────────────────────────────────────────────┐ │
│  │ register / login / me + get_current_user 依赖               │ │
│  │（Bearer JWT 或 NEXUS_AUTH_ENABLED=false 时 X-User-Id）      │ │
│  └────────────────────────────────────────────────────────────┘ │
│  ┌─ CRUD 层 ──────────────────────────────────────────────────┐ │
│  │ agents / sessions / messages / static-memory / timeline    │ │
│  │ llm-config / kv-memory / relationship / player-character   │ │
│  └────────────────────────────────────────────────────────────┘ │
│  ┌─ 聊天流水线 ───────────────────────────────────────────────┐ │
│  │ /chat → _build_chat_context → llm_chat / _chat_sse_stream  │ │
│  │     → 后台任务 _post_chat_tasks                            │ │
│  └────────────────────────────────────────────────────────────┘ │
└───────────────┬─────────────────────────────────────────────────┘
                │ 模块依赖
    ┌───────────┼───────────────┬───────────────┬───────────────┐
    ▼           ▼               ▼               ▼               ▼
database.py   llm.py        memory.py     prompt_templates.py  security.py
 (SQLite 层)  (LLM 网关)    (Mem0 封装)     (Prompt 构建)      (密码+JWT)
    │           │               │                               │
    ▼           ▼               ▼                               ▼
 config.py  (openai SDK)  (mem0ai/chromadb)              (bcrypt/jose)
    ▲
    └── 全局配置入口（.env / 环境变量），所有模块依赖
```

**依赖方向**（`import` 关系）：

```
main.py            → database, memory, config, llm, prompt_templates, security
database.py        → config
llm.py             → config, openai(AsyncOpenAI)
memory.py          → config, mem0.Memory（可选）
security.py        → config, bcrypt, jose
prompt_templates.py→ 无外部依赖（纯模板函数）
memory_manager.py  → 独立模块，无 import 方（历史遗留，见 §6.7）
```

---

## 4. 模块职责

### 4.1 `main.py` — FastAPI 主入口（核心，约 1000 行）

承载全部 HTTP 端点与聊天/抽取/摘要流水线。按功能区分为：

| 区块 | 内容 |
|---|---|
| 应用装配 | `lifespan`（启动建表 + Mem0 探测）、CORS、统一异常处理 |
| Pydantic 请求体 | `RegisterRequest` … `PlayerCharacterUpsert`（见 §5.1） |
| 认证与权限 | `get_current_user` / `get_owned_agent` / `get_owned_session` / `load_user_llm_row` |
| 认证端点 | `/auth/register` `/auth/login` `/auth/me` |
| 角色卡 CRUD | `/agents`（创建/列表/详情/更新/删除，删除级联清理） |
| 会话 CRUD | `/sessions`（创建/列表/详情/删除） + `/sessions/{id}/messages` |
| 聊天主入口 | `POST /chat`（JSON 或 SSE 流式） |
| 后台任务 | `_post_chat_tasks` → `_extract_story_event` / `_log_emotion` / `_refresh_summary_if_needed` |
| 记忆与剧情 CRUD | `/static-memory/*`、`/timeline/*`、`/kv-memory/*`、`/relationship/*`、`/player-character` |
| 用户 LLM 配置 | `/llm-config`（PUT upsert / GET 脱敏返回） |
| 健康检查 | `/health`（含 Mem0 / LLM Key / 认证状态） |

### 4.2 `database.py` — SQLite 异步访问层

- `_connect()`：每操作独立连接（WAL 模式、`PRAGMA foreign_keys=ON`、`row_factory=Row`），避免跨线程共享连接。
- `init_db()`：启动时幂等执行 `schema.sql`（`CREATE TABLE IF NOT EXISTS`）。
- 同步原子操作：`_query` / `_query_one` / `_execute`（返回 `lastrowid`）/ `_execute_many`（单事务，失败整体回滚）。
- 异步包装：`aquery` / `aquery_one` / `aexecute` / `aexecute_many`（经 `run_in_threadpool`），FastAPI 端点直接 `await`。

### 4.3 `llm.py` — LLM 网关

统一封装 OpenAI 兼容协议：

- `LLMConfig`（dataclass）：`model` / `api_key` / `base_url` / `provider`。
- `_clients` 缓存：以 `(base_url, api_key)` 为键复用 `AsyncOpenAI` 客户端。
- `resolve_llm_config(row)`：**用户自定义配置 > 环境变量 > 抛 `LLMError`**。
- `chat()`：普通对话，返回文本。
- `chat_stream()`：流式对话，`async for` 逐段 yield。
- `chat_json()` / `extract_json()`：结构化抽取；`extract_json` 兼容 ```` ```json ```` 围栏、前导噪声与多 JSON 对象扫描（`raw_decode`），解析失败返回 `None` 由调用方降级。

### 4.4 `memory.py` — Mem0 动态记忆封装（仅 Cakumi 用）

- 惰性初始化 `_init_memory()`：`NEXUS_MEM0_ENABLED=false` / 未安装 `mem0ai` / 未配 `NEXUS_LLM_API_KEY` / 初始化异常 → 记录 `_unavailable_reason`，**整体降级不阻塞启动**。
- 配置：Chroma 向量库（`NEXUS_CHROMA_PATH`）+ OpenAI 兼容 LLM。
- `search(query, user_id, agent_id, limit)` → RAG 检索文本（失败返回「（暂无相关记忆）」占位）。
- `add(text, user_id, agent_id)` → 写入动态记忆（失败静默）。
- `is_available()` → 健康检查与 `/health` 使用。

### 4.5 `prompt_templates.py` — Prompt 构建器

- `build_cakumi_messages(...)`：system 组装优先级为 **静态人设（最高）> 外观/基础人设/作者备注/属性面板/说话风格 > 关系状态 > 情感基调 > RAG 记忆 > KV 关键事实 > 早期摘要**，尾部接窗口化历史 + 用户输入。
- `build_mufy_messages(...)`：**叙事引导 > 可选静态人设 > 外观/风格/关系 > 故事时间线 > 基础人设 > 早期摘要 > 叙事要求**。
- 后台任务 Prompt 常量：`EXTRACT_STORY_EVENT_PROMPT`（剧情事件抽取 JSON）、`CONFLICT_CHECK_PROMPT`（人设冲突检测）、`SUMMARY_PROMPT`（增量摘要融合）、`MEMORY_CONSOLIDATION_PROMPT`（KV 记忆合并 ADD/UPDATE/DELETE/NOOP，预留）、`EMOTION_ANALYSIS_PROMPT`（情感分析 JSON）。

### 4.6 `security.py` — 认证

- `_prehash()`：密码先 sha256 再 bcrypt，规避 bcrypt 72 字节上限。
- `hash_password` / `verify_password`：bcrypt 直用（不引入 passlib，避开版本探测缺陷）。
- `create_access_token` / `decode_access_token`：HS256 JWT，`sub` 携带 user_id，过期时间 `NEXUS_TOKEN_EXPIRE_MINUTES`。

### 4.7 `config.py` — 全局配置

`Settings` 单例（`load_dotenv()` 后读取环境变量），`_bool` / `_int` / `_float` 容错解析。关键分组：服务（HOST/PORT）、安全（SECRET_KEY/TOKEN_EXPIRE/AUTH_ENABLED）、存储（DB_PATH/CHROMA_PATH/journal 模式）、LLM 默认配置、上下文管理（RECENT_KEEP/SUMMARY_THRESHOLD/RAG_TOP_K）、功能开关（MEM0_ENABLED/CONFLICT_CHECK_ENABLED）。

### 4.8 `memory_manager.py` — 记忆管理器（独立/历史遗留）

设计上融合 HerAndHim（KV 记忆 + consolidation + 关系状态 + 情感日志）、AstrBot（滚动摘要）、Mem0（RAG）。提供 `MemoryManager` 类：`get_static_memories` / `remember` / `forget` / `get_relationship` / `update_relationship` / `log_emotion` / `maybe_compress` / `build_boot_context` 等。

> ⚠️ **注意**：该模块**未接入 `main.py`**（`grep memory_manager main.py` 无结果），其表字段约定（`conversation_id`、`static_memory.key` 等）与当前 `schema.sql`（`session_id`、`static_memory.content`）不一致，属于设计演进中的早期方案，当前运行路径不依赖它。修改前请确认不会影响主流程。

---

## 5. 关键类与函数说明

### 5.1 Pydantic 请求体（`main.py`）

| 模型 | 字段要点 | 用途 |
|---|---|---|
| `RegisterRequest` | username 2-32 / password 6-128 | 注册 |
| `LoginRequest` | username / password | 登录 |
| `AgentCreate` / `AgentUpdate` | name, avatar, persona_prompt, opening_message + Mufy 富字段（gender/appearance/author_note/sample_dialogue/sidebar_data/tags） | 角色卡 |
| `SessionCreate` | agent_id, session_name, **session_type ∈ {cakumi, mufy}**, inherit_static_memory | 创建会话 |
| `ChatRequest` | session_id, user_input ≤4000, stream | 聊天 |
| `StaticMemoryAdd/Update` | content ≤2000, note ≤200 | 静态记忆 |
| `TimelineEventAdd` | session_id, summary ≤500, tags, parent_id, branch_id | 手动剧情事件 |
| `LLMConfigUpdate` | model, api_key, base_url, provider | 用户 LLM 配置 |
| `KVMemoryAdd` | agent_id, key ≤64, value ≤1000 | KV 记忆 |
| `RelationshipUpdate` | intimacy_delta, stage ≤32 | 关系状态 |
| `PlayerCharacterUpsert` | name, gender, persona | 玩家面具 |

### 5.2 认证/权限依赖

| 函数 | 说明 |
|---|---|
| `get_current_user` | 认证开启时校验 `Authorization: Bearer <token>`；关闭时用 `X-User-Id`，缺省取第一个用户。返回 `CurrentUser` |
| `get_owned_agent(agent_id, user)` | 校验角色存在且属于当前用户，否则 404 |
| `get_owned_session(session_id, user)` | 同上，会话维度 |
| `load_user_llm_row(user_id)` | 取该用户最新一条 `user_llm_config` |

### 5.3 聊天流水线（核心路径）

```
POST /chat
  └─ get_owned_session 校验
  └─ _build_chat_context(session, user, user_input)      ← 组装上下文
  │     ├─ 读 agent / static_memory / 窗口化历史 / session_summary
  │     ├─ 读 relationship_state / emotional_log(近5条均值) / kv_memory(近10条)
  │     ├─ cakumi → run_in_threadpool(mem0.search) → build_cakumi_messages
  │     └─ mufy   → 读 story_timeline → build_mufy_messages
  └─ _save_message(session_id, "user", user_input)       ← 用户消息先落库
  ├─ stream=false: resolve_llm_config → llm_chat → 保存 assistant
  │               → background_tasks.add(_post_chat_tasks)
  └─ stream=true:  StreamingResponse(_chat_sse_stream)
                  meta → delta* → done/error（生成器内完成后置任务）
```

**SSE 协议**（`_sse()` 序列化，`data: {json}\n\n`）：

| 事件 | 字段 |
|---|---|
| `meta` | session_id, session_type, prompt_used |
| `delta` | text |
| `done` | reply, message_id |
| `error` | message |

### 5.4 后台任务

| 函数 | 触发条件 | 行为 |
|---|---|---|
| `_post_chat_tasks` | 每轮聊完后台 | 按模式分发 + 情感日志 + 摘要刷新，异常仅记日志不阻塞 |
| `_extract_story_event` | 仅 mufy | LLM 判定 `need_save` → 解析 JSON（summary/tags/parent/topic/sentiment）→ 可选 `CONFLICT_CHECK_ENABLED` 冲突检测 → 写 `story_timeline`（is_manual=0） |
| `_conflicts_static_memory` | 冲突检测开关+继承人设 | 返回 `{"conflict": bool}` |
| `_log_emotion` | 每轮（v2.1） | 情感分析写 `emotional_log`；sentiment>0.3 亲密度+1，<-0.3 -1（0-100 夹取） |
| `_refresh_summary_if_needed` | 每轮（v2.1） | 消息数>阈值且摘要未覆盖窗口边界时，增量融合旧摘要（`_summary_locks` 每会话锁 + 双重检查防并发重复压缩） |

### 5.5 LLM 网关（`llm.py`）

| 符号 | 说明 |
|---|---|
| `LLMError` | 未配置 Key / 网络错误 / 空返回的统一异常，映射 HTTP 502 |
| `LLMConfig` | 连接配置 dataclass |
| `resolve_llm_config` | 用户配置 > 环境变量 > 抛错 |
| `chat / chat_stream / chat_json` | 三种调用形态 |
| `extract_json` | 健壮 JSON 提取（围栏剥离 + 全串扫描 raw_decode） |

### 5.6 记忆/安全/配置

| 模块 | 符号 | 说明 |
|---|---|---|
| memory.py | `_init_memory` / `is_available` / `search` / `add` | Mem0 惰性初始化与 RAG 读写（降级安全） |
| security.py | `hash_password` / `verify_password` / `create_access_token` / `decode_access_token` | 密码哈希与 JWT |
| config.py | `Settings` / `settings` 单例 | 全部环境变量 |

---

## 6. 数据模型（`schema.sql`）

| 表 | 作用 | 关键字段 / 约束 |
|---|---|---|
| `users` | 用户 | username UNIQUE, password_hash |
| `user_llm_config` | 每用户 LLM 配置 | model_provider / model_name / api_key / base_url，FK→users |
| `agents` | 角色卡（Mufy 富卡） | persona_prompt、gender/appearance/author_note/sample_dialogue/sidebar_data、tags(JSON) |
| `player_character` | 玩家面具 | UNIQUE(user_id) |
| `sessions` | 会话 | **session_type**（cakumi/mufy，创建后不可改）、inherit_static_memory |
| `messages` | 聊天消息 | role / content，索引 idx_messages_session |
| `static_memory` | Cakumi 静态人设（按 agent 共享） | content / note |
| `story_timeline` | Mufy 剧情事件 | event_summary、tags、causal_parent_id、**branch_id**（分支预留）、is_manual、sentiment、topic |
| `session_summary` | 滚动摘要（每会话唯一） | session_id UNIQUE、summary_text、covered_until_msg_id |
| `kv_memory` | KV 动态记忆（HerAndHim） | UNIQUE(user_id, agent_id, key)，value 最新覆盖 |
| `relationship_state` | 关系状态 | UNIQUE(user_id, agent_id)、intimacy_score 0-100、stage |
| `emotional_log` | 情感时间序列日志 | sentiment(-1~1)、emotion_tag |

**数据隔离矩阵**（绑定键 → 可见范围）：

| 数据 | 绑定键 | 可见会话 |
|---|---|---|
| static_memory | agent_id | 该角色所有会话共享 |
| Mem0 动态记忆 | user_id + agent_id | 仅 cakumi |
| story_timeline | session_id | 仅 mufy |
| session_summary | session_id | 当前会话 |
| user_llm_config | user_id | 该用户全部请求 |
| messages | session_id | 当前会话 |

**级联删除**：删角色 → 消息→时间线→摘要→会话→静态记忆→角色（`aexecute_many` 单事务）；删会话 → 消息/时间线/摘要/会话。

---

## 7. 依赖关系

### 7.1 第三方依赖（`requirements.txt`）

| 包 | 用途 |
|---|---|
| fastapi / uvicorn[standard] | Web 框架与 ASGI 服务器 |
| pydantic | 请求/响应模型校验 |
| python-multipart / python-dotenv | 表单与 .env 加载 |
| openai | OpenAI 兼容 LLM 网关（AsyncOpenAI） |
| httpx | openai 底层传输 |
| bcrypt | 密码哈希 |
| python-jose[cryptography] | JWT 编解码 |
| mem0ai / chromadb（**可选**） | 向量 RAG 动态记忆；缺失自动降级 |

### 7.2 模块依赖图

```
config.py ◄── 全部模块（唯一直读环境变量的模块）
main.py ◄── database / llm / memory / prompt_templates / security
database.py ◄── config
llm.py ◄── config + openai
memory.py ◄── config + mem0(可选) + chromadb(可选)
security.py ◄── config + bcrypt + jose
```

### 7.3 参考项目（子目录开源代码引用说明）

以下 5 个子目录是**上游开源项目的完整克隆**（各自含 `.git`，与主程序同仓库共存）。主程序运行时**不会 import** 其中任何模块——它们的价值是作为**设计参考源码**：`main.py` / `schema.sql` / `prompt_templates.py` 中的相关实现，是在研读这些源码后按本项目技术栈（FastAPI + SQLite）重新落地，而非代码级拷贝（仅有 `mem0` 通过 pip 安装的 `mem0ai` 包在运行时被调用）。

> 许可证注意：`AstrBot/` 与 `HerAndHim/` 为 **AGPL-3.0**（有传染性）；本仓库自身已采用 **AGPL-3.0**（见根目录 `LICENSE`），与参考项目许可一致，对外分发或商用派生作品时请遵守 AGPL-3.0 义务（如公开源代码）；`letta/`、`mem0/` 为 Apache-2.0（宽松友好）。

#### 7.3.1 HerAndHim（核心借鉴源，v2.1 主要融合对象）

- 上游：[github.com/ericwang915/HerAndHim](https://github.com/ericwang915/HerAndHim) ｜ 许可证：AGPL-3.0
- 角色：开源 AI 伴侣，提供「长期记忆 + 情感 + 关系」完整设计，是 v2.1 三层记忆增强的直接来源

| 参考文件（子目录内） | 借鉴设计 | 本项目落地位置 |
|---|---|---|
| [manager.py](file:///Users/nan/ai-kitten/HerAndHim/herandhim/core/memory/manager.py) | KV 长期记忆：last-write-wins，按 key 最新值覆盖 | `kv_memory` 表 + [main.py](file:///Users/nan/ai-kitten/main.py) `/kv-memory/*` 端点 |
| [consolidation.py](file:///Users/nan/ai-kitten/HerAndHim/herandhim/core/memory/consolidation.py) | 写前 LLM 合并去重：ADD / UPDATE / DELETE / NOOP（Mem0-lite） | `MEMORY_CONSOLIDATION_PROMPT`（[prompt_templates.py](file:///Users/nan/ai-kitten/prompt_templates.py)，预留未启用） |
| [emotional_graph.py](file:///Users/nan/ai-kitten/HerAndHim/herandhim/core/memory/emotional_graph.py) | SentimentAnalyzer（情感分析）/ EmotionalGraph（情感时序日志）/ RelationshipStore（亲密度+关系阶段） | `emotional_log`、`relationship_state` 表 + `_log_emotion()`、`/relationship/*` 端点 |
| [temporal_index.py](file:///Users/nan/ai-kitten/HerAndHim/herandhim/core/memory/temporal_index.py) | 时间线事件模型（topic / summary / sentiment / keywords 字段） | `story_timeline.sentiment/topic` 字段 + `EXTRACT_STORY_EVENT_PROMPT` |

#### 7.3.2 AstrBot

- 上游：Soulter/AstrBot（多平台聊天机器人框架）｜ 许可证：AGPL-3.0
- 角色：提供对话历史持久化与上下文管理参考
- 参考：[history_saver.py](file:///Users/nan/ai-kitten/AstrBot/astrbot/core/utils/history_saver.py)（`persist_agent_history` 等会话历史处理）→ 本项目落地为**长对话滚动摘要**：`session_summary` 表 + `_refresh_summary_if_needed()`（最近 `RECENT_KEEP` 条原文 + 早期摘要共同进 Prompt，超阈值后台增量压缩）。

#### 7.3.3 GirlfriendGPT

- 上游：EniasCailliau/GirlfriendGPT（starter 项目，上游仓库声明已停止维护）｜ 许可证：仓库未内置 LICENSE 文件
- 角色：产品形态参考——「个性化 AI 伴侣」定位（角色人设、语音、Telegram 接入等），启发本项目「AI 伴侣 + 角色卡」产品方向；代码层面未直接落地。

#### 7.3.4 letta

- 上游：letta-ai/letta-code（原 MemGPT 项目）｜ 许可证：Apache-2.0
- 角色：stateful agents with memory——记忆分层 / 上下文窗口管理 / 长期记忆自改进思想，启发本项目「静态人设 + 滚动摘要 + RAG」的多层上下文设计；代码层面未直接落地。

#### 7.3.5 mem0

- 上游：mem0ai/mem0 ｜ 许可证：Apache-2.0
- 角色：向量 RAG 记忆引擎。运行时经 pip 安装的 `mem0ai` 包调用（[memory.py](file:///Users/nan/ai-kitten/memory.py)，Chroma 后端，缺失自动降级）；本子目录为源码克隆，供阅读接口行为（`Memory.search` / `Memory.add` 的参数、命名空间与降级语义）。

#### 7.3.6 融合对照表（对应 README v2.1 声明）

| 融合来源 | 借鉴内容 | 落地位置 |
|---|---|---|
| HerAndHim | KV 动态记忆 / 关系状态 / 情感日志 / 时间线 sentiment+topic | `kv_memory`、`relationship_state`、`emotional_log` 表 + 相应端点 |
| AstrBot | 长对话滚动摘要 | `session_summary` 表 + `_refresh_summary_if_needed` |
| mem0 | 向量 RAG 动态记忆 | [memory.py](file:///Users/nan/ai-kitten/memory.py)（仅 Cakumi 会话） |
| Mufy 思路（概念） | 时序剧情时间线 + 分支因果 | `story_timeline` 表（`branch_id` / `causal_parent_id` 预留） |

> 维护建议：这些目录均为完整 git 仓库，与主项目代码无 import 依赖；如需精简仓库体积可整体移除（移除后仅丢失「参考源码」价值，不影响运行）。

---

## 8. 配置项（环境变量，前缀 `NEXUS_`）

| 变量 | 默认 | 说明 |
|---|---|---|
| `NEXUS_HOST` / `NEXUS_PORT` | 127.0.0.1 / 8000 | 服务地址 |
| `NEXUS_SECRET_KEY` | dev 值 | JWT 密钥，生产必改 |
| `NEXUS_TOKEN_EXPIRE_MINUTES` | 10080 | Token 有效期 |
| `NEXUS_AUTH_ENABLED` | true | false 免登录（默认第一个用户） |
| `NEXUS_DB_PATH` / `NEXUS_CHROMA_PATH` | 项目目录 | DB / 向量库路径 |
| `NEXUS_SQLITE_JOURNAL_MODE` | WAL | WAL 并发最好；受限沙箱可 MEMORY/OFF |
| `NEXUS_LLM_API_KEY` / `NEXUS_LLM_BASE_URL` / `NEXUS_LLM_MODEL` | DeepSeek 默认 | 网关默认 LLM（用户级配置优先） |
| `NEXUS_LLM_TEMPERATURE` / `NEXUS_LLM_MAX_TOKENS` | 0.8 / 1024 | 生成参数 |
| `NEXUS_RECENT_KEEP` | 16 | 进入 Prompt 的最近原文条数 |
| `NEXUS_SUMMARY_THRESHOLD` | 30 | 触发滚动摘要的消息总数 |
| `NEXUS_RAG_TOP_K` | 5 | Mem0 检索条数 |
| `NEXUS_MEM0_ENABLED` | true | Mem0 开关 |
| `NEXUS_CONFLICT_CHECK_ENABLED` | false | Mufy 写时间线前的人设冲突检测 |

---

## 9. 运行方式

### 9.1 快速开始

```bash
# 1. 安装依赖（Python 3.10+）
pip install -r requirements.txt

# 2. 配置环境变量（不配置则稍后在 /llm-config 配 Key）
cp .env.example .env

# 3. 启动（schema.sql 启动时自动建表，无需手动初始化）
python main.py
# 或
uvicorn main:app --reload
```

### 9.2 验证

- 后端文档（Swagger）：<http://127.0.0.1:8000/docs>
- 健康检查：<http://127.0.0.1:8000/health>（返回 `mem0` / `llm_env_key` / `auth_enabled`）
- 最小链路（认证开启时）：

```bash
# 注册
curl -X POST http://127.0.0.1:8000/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"username":"demo","password":"demo123"}'
# 用返回的 access_token 依次：建角色 → 建会话 → 聊天
curl -X POST http://127.0.0.1:8000/chat \
  -H "Authorization: Bearer <token>" -H 'Content-Type: application/json' \
  -d '{"session_id":1,"user_input":"你好","stream":false}'
```

### 9.3 开发模式

- `NEXUS_AUTH_ENABLED=false` 免登录，自动使用第一个用户（无用户则先调用 `/auth/register`）。
- `main.py` 以 `uvicorn ... reload=True` 启动，改代码热重载。
- 未装 `mem0ai` / 未配 LLM Key 时应用仍可启动（记忆降级为占位文本，聊天需先配 LLM）。

---

## 10. API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/auth/register` `/auth/login` | 注册/登录（返回 access_token） |
| GET | `/auth/me` | 当前用户 |
| POST/GET | `/agents` | 创建/列表角色卡 |
| GET/PATCH/DELETE | `/agents/{id}` | 角色卡详情/更新/删除（级联） |
| POST/GET | `/sessions` | 创建（session_type 创建后不可改）/列表（可按 agent_id 过滤） |
| GET/DELETE | `/sessions/{id}` | 详情/删除（级联） |
| GET | `/sessions/{id}/messages` | 消息正序（limit 1-1000，默认 200） |
| POST | `/chat` | 聊天；`stream=true` 走 SSE（meta→delta*→done/error） |
| POST/GET | `/static-memory/add` `/static-memory/list` | 静态记忆增/查 |
| PATCH/DELETE | `/static-memory/{id}` | 静态记忆改/删 |
| POST/GET | `/timeline/add-event` `/timeline/list` | 手动剧情事件增/查 |
| DELETE | `/timeline/events/{id}` | 删除剧情事件 |
| PUT/GET | `/llm-config` | 用户 LLM 配置 upsert / 脱敏查询 |
| POST/GET/DELETE | `/kv-memory/*` | KV 动态记忆增/查/删 |
| GET/PATCH | `/relationship/{agent_id}` | 关系状态查询/亲密度与阶段更新 |
| GET/PUT | `/player-character` | 玩家面具查询/upsert |
| GET | `/health` | 健康检查 |

---

## 11. 关键流程速览

1. **新建会话**：登录 → 选/建角色 → 选模式（cakumi/mufy，mufy 可勾选继承静态人设）→ `POST /sessions`。
2. **Cakumi 聊天**：user 消息落库 → 读静态人设 + Mem0 RAG → `build_cakumi_messages` → LLM 回复落库 → 后台 Mem0 抽取细碎记忆 + 情感日志 + 摘要刷新；**不碰 timeline**。
3. **Mufy 聊天**：user 消息落库 → 读时间线 → `build_mufy_messages` → 回复落库 → 后台剧情事件抽取（+可选冲突检测）；**不调 Mem0**。
4. **长对话**：最近 `RECENT_KEEP` 条原文 + 早期 `session_summary` 滚动摘要共同进入上下文，后台按阈值增量压缩。

---

## 12. 常见开发注意点

- **并发安全**：SQLite 用「每次操作独立连接 + WAL」规避跨线程问题；摘要压缩用 `_summary_locks` + 双重检查防并发重复。
- **LLM 失败语义**：`LLMError` 统一 502；聊天失败不丢用户消息（先落库）；后台任务异常仅记日志。
- **降级路径**：Mem0 缺失/未配置 → 检索占位文本、写入静默、启动不受阻（`/health` 显示 `degraded`）。
- **扩展预留**：`story_timeline.branch_id` / `causal_parent_id` 支持剧情分支树；`MEMORY_CONSOLIDATION_PROMPT` 为 KV 记忆自动合并预留；`user_llm_config` 可平滑加按会话选模型。

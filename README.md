# NexusCompanion

自研 AI 伴侣 Web 应用｜双模式会话隔离

- **Cakumi 人设锁会话 (session_type = cakumi)**：静态显式人设记忆 + Mem0 RAG 动态人物记忆，自动记住细碎信息，不生成剧情时间线，目标是锁人设、防 OOC。
- **长篇叙事会话 (session_type = mufy)**：时序剧情事件时间线，支持事件因果、剧情分支、存档跳转；不使用 Mem0 动态记忆，可选择是否继承角色静态人设，支持剧情改变人物状态。

> 重要：两种会话数据隔离，同一角色可并行创建多条不同类型会话，互不干扰。

## v2 更新（相对骨架版）

| 能力 | 骨架版 | v2 |
|---|---|---|
| LLM 调用 | 占位符 TODO | OpenAI 兼容网关（DeepSeek/Ollama/OneAPI…），支持每用户 Key、温度/Token 配置 |
| 流式 | 无 | `POST /chat` 加 `stream=true` 即返回 SSE 流 |
| 剧情事件抽取 | `pass` 空实现 | 真实跑通：LLM 判定 → JSON 解析 → 写入时间线，可选人设冲突检测 |
| 认证 | 无 | 注册 / 登录 / JWT，全端点鉴权（`NEXUS_AUTH_ENABLED=false` 可免登录开发） |
| 长上下文 | 全量历史拼接 | 窗口化（最近 16 条原文）+ 滚动摘要（`session_summary` 表，后台异步刷新） |
| CRUD | 仅静态记忆/时间线 | 用户、角色卡、会话、消息、静态记忆、时间线、LLM 配置全覆盖 |
| 健壮性 | agent 为空会 500 | 统一 404/409/502 语义、事务级联删除、Mem0 缺失自动降级 |

## v2.1 融合开源项目优点（当前版本）

| 融合来源 | 抄了什么 | 落地位置 |
|---|---|---|
| **HerAndHim** | KV 动态记忆（最新值覆盖旧值、自动去重） | `kv_memory` 表 + `/kv-memory/*` 接口 |
| **HerAndHim** | 关系状态（亲密度 0-100 + 关系阶段） | `relationship_state` 表 + `/relationship/*` 接口 |
| **HerAndHim** | 情感时间序列日志（每轮对话自动分析情感） | `emotional_log` 表 + `_log_emotion()` 后台任务 |
| **HerAndHim** | 时间线事件加 sentiment/topic 字段 | `story_timeline` 表 + 剧情抽取 Prompt |
| **AstrBot** | 长对话滚动摘要（最近N条原文 + 早期摘要） | `session_summary` 表（已有，保留） |
| **mem0** | 向量 RAG 检索（动态人物记忆） | `memory.py`（已有，保留） |
| **长篇叙事思路** | 时序剧情时间线 + 分支因果 | `story_timeline` 表（已有，增强字段） |

> 新增后台任务：每轮聊天结束后，自动调用 LLM 分析情感，写入 `emotional_log`，并根据情感分微调亲密度（正向+1/负向-1）。

## 技术栈

- 后端：Python FastAPI（异步 + 线程池 SQLite）
- 数据库：SQLite（原型，WAL 模式）/ PostgreSQL（后期）
- 向量库：Chroma（本地文件，经 Mem0）
- 记忆引擎：Mem0（可选依赖，缺失自动降级）
- 前端：Vue3 + Element Plus + Pinia
- 流式通信：SSE
- LLM 网关：用户自定义 API Key / Ollama 本地内置模型

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置环境变量（复制示例后按需修改；不配置则稍后在 /llm-config 里配 Key）
cp .env.example .env

# 3. 启动（schema.sql 会在启动时自动执行建表，无需手动初始化数据库）
python main.py
# 或
uvicorn main:app --reload
```

后端文档：http://127.0.0.1:8000/docs

## 配置项（环境变量）

| 变量 | 默认 | 说明 |
|---|---|---|
| `NEXUS_SECRET_KEY` | dev 值 | JWT 签名密钥，生产必改 |
| `NEXUS_AUTH_ENABLED` | true | false 时免登录（默认第一个用户） |
| `NEXUS_LLM_API_KEY` / `NEXUS_LLM_BASE_URL` / `NEXUS_LLM_MODEL` | DeepSeek | 网关默认 LLM，用户级配置优先 |
| `NEXUS_DB_PATH` / `NEXUS_CHROMA_PATH` | 项目目录 | 数据库 / 向量库路径 |
| `NEXUS_RECENT_KEEP` | 16 | 进入 Prompt 的最近消息条数 |
| `NEXUS_SUMMARY_THRESHOLD` | 30 | 触发滚动摘要的消息总数阈值 |
| `NEXUS_CONFLICT_CHECK_ENABLED` | false | 写时间线前是否校验人设冲突 |
| `NEXUS_MEM0_ENABLED` | true | Mem0 开关 |

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/auth/register` `/auth/login` | 注册 / 登录，返回 `access_token` |
| GET | `/auth/me` | 当前用户 |
| POST/GET | `/agents` | 角色卡创建 / 列表 |
| GET/PATCH/DELETE | `/agents/{id}` | 角色卡详情 / 更新 / 删除（级联清理） |
| POST/GET | `/sessions` | 创建会话（`session_type` 创建后不可改）/ 列表 |
| GET/DELETE | `/sessions/{id}` | 会话详情 / 删除（级联清理消息、时间线、摘要） |
| GET | `/sessions/{id}/messages` | 会话消息（正序） |
| POST | `/chat` | 聊天；`stream=true` 返回 SSE（`meta` → `delta`* → `done`） |
| POST/GET | `/static-memory/add` `/static-memory/list` | 静态人设记忆 增 / 查 |
| PATCH/DELETE | `/static-memory/{id}` | 静态记忆 改 / 删 |
| POST/GET | `/timeline/add-event` `/timeline/list` | 剧情事件 增 / 查 |
| DELETE | `/timeline/events/{id}` | 删除剧情事件 |
| PUT/GET | `/llm-config` | 用户 LLM Key 配置（GET 返回脱敏 Key） |
| GET | `/health` | 健康检查（含 Mem0 状态） |

## 会话规则

1. `static_memory`（静态人设记忆）绑定 `agent_id`，该角色所有会话共享；叙事会话可开关是否加载。
2. Cakumi 会话：使用 Mem0，不读写 `story_timeline`。
3. 长篇叙事会话：使用 `story_timeline`，不调用 Mem0 抽取记忆。
4. 会话一旦创建，`session_type` 不可修改；需要切换模式只能新建会话。

## 目录结构

```
NexusCompanion/
├── main.py               # FastAPI 主入口：全部端点 + 聊天/抽取/摘要流水线
├── config.py             # 环境变量配置（.env）
├── database.py           # SQLite 异步访问层 + 启动自动建表
├── llm.py                # LLM 网关（chat / stream / JSON 抽取）
├── security.py           # bcrypt + JWT 认证
├── memory.py             # Mem0 封装（缺失自动降级）
├── prompt_templates.py   # Prompt 构建器（system + 历史 + 输入）与后台任务 Prompt
├── requirements.txt
├── schema.sql            # 建表 SQL（启动时自动执行）
├── session_flow.md       # 业务流程文档
├── .env.example          # 环境变量示例
├── README.md
├── chroma_db/            # Mem0 向量库（运行后自动生成）
└── sqlite.db             # SQLite 数据库（运行后自动生成）
```

## 后续路线

- 阶段 1：基础底座 + Cakumi 人设锁会话 ✅
- 阶段 2：长篇叙事会话模块（时间线、剧情事件、分支存档）✅（抽取已跑通，分支字段已预留）
- 阶段 3：LLM 网关接入 Ollama 内置模型（配置即用）
- 阶段 4：长对话摘要 ✅ / UI 打磨 / 角色卡导入导出（JSON）
- 阶段 5：PostgreSQL 迁移、多端登录、对话导出

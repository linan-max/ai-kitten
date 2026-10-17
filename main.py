"""
NexusCompanion 后端主入口（v2）

双模式会话隔离：
- cakumi 人设锁会话：静态人设 + Mem0 RAG 动态记忆，不写剧情时间线；
- mufy   长篇叙事会话：剧情时间线（自动抽取 / 因果 / 冲突检测），不写 Mem0。

v2 相对骨架版的变化：
1. LLM 网关落地：OpenAI 兼容协议（DeepSeek/Ollama/OneAPI…），支持每用户 API Key、SSE 流式；
2. 补齐认证（注册/登录/JWT）与角色卡、会话、消息的完整 CRUD；
3. Mufy 剧情事件自动抽取真正跑通，可选静态人设冲突检测；
4. 长对话滚动摘要（session_summary），上下文窗口化，不再无脑拼接全量历史；
5. 统一异常处理、CORS、健康检查；Mem0 依赖缺失时自动降级不阻塞启动。
"""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from typing import Literal, Optional

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

import database as db
import memory as mem0_client
from config import settings
from llm import LLMError, chat as llm_chat, chat_json, chat_stream, resolve_llm_config
from prompt_templates import (
    CONFLICT_CHECK_PROMPT,
    EMOTION_ANALYSIS_PROMPT,
    EXTRACT_STORY_EVENT_PROMPT,
    MEMORY_CONSOLIDATION_PROMPT,
    SUMMARY_PROMPT,
    build_cakumi_messages,
    build_mufy_messages,
)
from security import create_access_token, decode_access_token, hash_password, verify_password

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s | %(message)s"
)
logger = logging.getLogger("nexus")


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    logger.info("数据库已就绪: %s", settings.DB_PATH)
    logger.info("Mem0 动态记忆: %s", "可用" if mem0_client.is_available() else "不可用（已降级）")
    yield


app = FastAPI(title=settings.APP_NAME, version="2.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ========== 请求体 ==========

class RegisterRequest(BaseModel):
    username: str = Field(min_length=2, max_length=32)
    password: str = Field(min_length=6, max_length=128)


class LoginRequest(BaseModel):
    username: str
    password: str


class AgentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    avatar: Optional[str] = None
    persona_prompt: Optional[str] = None
    opening_message: Optional[str] = None
    # Mufy 式富角色卡字段
    gender: Optional[str] = Field(default=None, max_length=16)
    appearance: Optional[str] = Field(default=None, max_length=2000)
    author_note: Optional[str] = Field(default=None, max_length=2000)
    sample_dialogue: Optional[str] = Field(default=None, max_length=4000)
    sidebar_data: Optional[str] = None
    tags: list[str] = Field(default_factory=list)


class AgentUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=64)
    avatar: Optional[str] = None
    persona_prompt: Optional[str] = None
    opening_message: Optional[str] = None
    gender: Optional[str] = Field(default=None, max_length=16)
    appearance: Optional[str] = None
    author_note: Optional[str] = None
    sample_dialogue: Optional[str] = None
    sidebar_data: Optional[str] = None
    tags: Optional[list[str]] = None


class SessionCreate(BaseModel):
    agent_id: int
    session_name: Optional[str] = Field(default=None, max_length=64)
    session_type: Literal["cakumi", "mufy"] = "cakumi"
    # 仅 mufy 生效：是否继承角色静态人设
    inherit_static_memory: bool = True


class ChatRequest(BaseModel):
    session_id: int
    user_input: str = Field(min_length=1, max_length=4000)
    # stream=true 时返回 SSE 流（text/event-stream），否则返回 JSON
    stream: bool = False


class StaticMemoryAdd(BaseModel):
    agent_id: int
    content: str = Field(min_length=1, max_length=2000)
    note: Optional[str] = Field(default=None, max_length=200)


class StaticMemoryUpdate(BaseModel):
    content: Optional[str] = Field(default=None, min_length=1, max_length=2000)
    note: Optional[str] = Field(default=None, max_length=200)


class TimelineEventAdd(BaseModel):
    session_id: int
    summary: str = Field(min_length=1, max_length=500)
    tags: list[str] = Field(default_factory=list)
    parent_id: Optional[int] = None
    branch_id: str = Field(default="main", max_length=32)


class LLMConfigUpdate(BaseModel):
    model: str = Field(min_length=1, max_length=128)
    api_key: str = Field(min_length=1, max_length=512)
    base_url: Optional[str] = Field(default=None, max_length=256)
    provider: Optional[str] = Field(default="openai-compatible", max_length=64)


class CurrentUser(BaseModel):
    id: int
    username: str


# ========== 认证依赖 ==========

async def get_current_user(
    authorization: str = Header(default=""),
    x_user_id: str = Header(default=""),
) -> CurrentUser:
    """解析当前用户。

    - 认证开启（默认）：要求 Authorization: Bearer <token>；
    - 认证关闭（NEXUS_AUTH_ENABLED=false，仅限原型开发）：可用 X-User-Id 指定，默认取第一个用户。
    """
    user_id: Optional[int] = None
    if not settings.AUTH_ENABLED:
        user_id = int(x_user_id) if x_user_id.isdigit() else None
        if user_id is None:
            row = await db.aquery_one("SELECT id, username FROM users ORDER BY id LIMIT 1")
            if row:
                user_id = row["id"]
        if user_id is None:
            raise HTTPException(401, "开发模式下数据库中还没有用户，请先调用 POST /auth/register")
    else:
        if not authorization.startswith("Bearer "):
            raise HTTPException(401, "缺少 Authorization: Bearer <token>")
        user_id = decode_access_token(authorization[len("Bearer "):].strip())
        if user_id is None:
            raise HTTPException(401, "Token 无效或已过期，请重新登录")

    row = await db.aquery_one("SELECT id, username FROM users WHERE id=?", (user_id,))
    if not row:
        raise HTTPException(401, "用户不存在")
    return CurrentUser(**row)


# ========== 权限助手 ==========

async def get_owned_agent(agent_id: int, user: CurrentUser) -> dict:
    row = await db.aquery_one("SELECT * FROM agents WHERE id=?", (agent_id,))
    if not row or row["user_id"] != user.id:
        raise HTTPException(404, "角色不存在")
    return row


async def get_owned_session(session_id: int, user: CurrentUser) -> dict:
    row = await db.aquery_one("SELECT * FROM sessions WHERE id=?", (session_id,))
    if not row or row["user_id"] != user.id:
        raise HTTPException(404, "会话不存在")
    return row


async def load_user_llm_row(user_id: int) -> Optional[dict]:
    return await db.aquery_one(
        "SELECT * FROM user_llm_config WHERE user_id=? ORDER BY id DESC LIMIT 1", (user_id,)
    )


# ========== 认证 ==========

@app.post("/auth/register")
async def register(req: RegisterRequest):
    if await db.aquery_one("SELECT id FROM users WHERE username=?", (req.username,)):
        raise HTTPException(409, "用户名已被注册")
    user_id = await db.aexecute(
        "INSERT INTO users(username, password_hash) VALUES (?,?)",
        (req.username, hash_password(req.password)),
    )
    return {"user_id": user_id, "access_token": create_access_token(user_id)}


@app.post("/auth/login")
async def login(req: LoginRequest):
    row = await db.aquery_one("SELECT * FROM users WHERE username=?", (req.username,))
    if not row or not verify_password(req.password, row["password_hash"]):
        raise HTTPException(401, "用户名或密码错误")
    return {"user_id": row["id"], "access_token": create_access_token(row["id"])}


@app.get("/auth/me")
async def me(user: CurrentUser = Depends(get_current_user)):
    return {"id": user.id, "username": user.username}


# ========== 角色卡 CRUD ==========

@app.post("/agents")
async def create_agent(req: AgentCreate, user: CurrentUser = Depends(get_current_user)):
    agent_id = await db.aexecute(
        """INSERT INTO agents(user_id, name, avatar, persona_prompt, opening_message,
                              gender, appearance, author_note, sample_dialogue, sidebar_data, tags)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (
            user.id, req.name, req.avatar, req.persona_prompt, req.opening_message,
            req.gender, req.appearance, req.author_note, req.sample_dialogue,
            req.sidebar_data, json.dumps(req.tags, ensure_ascii=False),
        ),
    )
    return {"id": agent_id}


@app.get("/agents")
async def list_agents(user: CurrentUser = Depends(get_current_user)):
    return await db.aquery("SELECT * FROM agents WHERE user_id=? ORDER BY id DESC", (user.id,))


@app.get("/agents/{agent_id}")
async def get_agent(agent_id: int, user: CurrentUser = Depends(get_current_user)):
    return await get_owned_agent(agent_id, user)


@app.patch("/agents/{agent_id}")
async def update_agent(
    agent_id: int, req: AgentUpdate, user: CurrentUser = Depends(get_current_user)
):
    await get_owned_agent(agent_id, user)
    await db.aexecute(
        """UPDATE agents SET
             name = COALESCE(?, name),
             avatar = COALESCE(?, avatar),
             persona_prompt = COALESCE(?, persona_prompt),
             opening_message = COALESCE(?, opening_message),
             gender = COALESCE(?, gender),
             appearance = COALESCE(?, appearance),
             author_note = COALESCE(?, author_note),
             sample_dialogue = COALESCE(?, sample_dialogue),
             sidebar_data = COALESCE(?, sidebar_data),
             tags = CASE WHEN ? IS NOT NULL THEN ? ELSE tags END
           WHERE id = ?""",
        (
            req.name, req.avatar, req.persona_prompt, req.opening_message,
            req.gender, req.appearance, req.author_note, req.sample_dialogue,
            req.sidebar_data,
            json.dumps(req.tags, ensure_ascii=False) if req.tags is not None else None,
            json.dumps(req.tags, ensure_ascii=False) if req.tags is not None else None,
            agent_id,
        ),
    )
    return {"ok": True}


@app.delete("/agents/{agent_id}")
async def delete_agent(agent_id: int, user: CurrentUser = Depends(get_current_user)):
    await get_owned_agent(agent_id, user)
    # 级联清理：消息 -> 剧情事件 -> 摘要 -> 会话 -> 静态记忆 -> 角色
    await db.aexecute_many(
        [
            ("DELETE FROM messages WHERE session_id IN (SELECT id FROM sessions WHERE agent_id=?)", (agent_id,)),
            ("DELETE FROM story_timeline WHERE agent_id=?", (agent_id,)),
            ("DELETE FROM session_summary WHERE session_id IN (SELECT id FROM sessions WHERE agent_id=?)", (agent_id,)),
            ("DELETE FROM sessions WHERE agent_id=?", (agent_id,)),
            ("DELETE FROM static_memory WHERE agent_id=?", (agent_id,)),
            ("DELETE FROM agents WHERE id=?", (agent_id,)),
        ]
    )
    return {"ok": True}


# ========== 会话 CRUD ==========

@app.post("/sessions")
async def create_session(req: SessionCreate, user: CurrentUser = Depends(get_current_user)):
    await get_owned_agent(req.agent_id, user)
    session_id = await db.aexecute(
        """INSERT INTO sessions(user_id, agent_id, session_name, session_type, inherit_static_memory)
           VALUES (?,?,?,?,?)""",
        (user.id, req.agent_id, req.session_name, req.session_type, int(req.inherit_static_memory)),
    )
    return {"id": session_id}


@app.get("/sessions")
async def list_sessions(
    agent_id: Optional[int] = None, user: CurrentUser = Depends(get_current_user)
):
    sql = """SELECT s.*, a.name AS agent_name
             FROM sessions s JOIN agents a ON a.id = s.agent_id
             WHERE s.user_id = ?"""
    params: tuple = (user.id,)
    if agent_id is not None:
        sql += " AND s.agent_id = ?"
        params += (agent_id,)
    sql += " ORDER BY s.id DESC"
    return await db.aquery(sql, params)


@app.get("/sessions/{session_id}")
async def get_session(session_id: int, user: CurrentUser = Depends(get_current_user)):
    row = await get_owned_session(session_id, user)
    agent = await db.aquery_one("SELECT id, name FROM agents WHERE id=?", (row["agent_id"],))
    return {**row, "agent_name": agent["name"] if agent else None}


@app.delete("/sessions/{session_id}")
async def delete_session(session_id: int, user: CurrentUser = Depends(get_current_user)):
    await get_owned_session(session_id, user)
    await db.aexecute_many(
        [
            ("DELETE FROM messages WHERE session_id=?", (session_id,)),
            ("DELETE FROM story_timeline WHERE session_id=?", (session_id,)),
            ("DELETE FROM session_summary WHERE session_id=?", (session_id,)),
            ("DELETE FROM sessions WHERE id=?", (session_id,)),
        ]
    )
    return {"ok": True}


@app.get("/sessions/{session_id}/messages")
async def list_messages(
    session_id: int,
    limit: int = Query(default=200, ge=1, le=1000),
    user: CurrentUser = Depends(get_current_user),
):
    await get_owned_session(session_id, user)
    rows = await db.aquery(
        "SELECT * FROM messages WHERE session_id=? ORDER BY id DESC LIMIT ?", (session_id, limit)
    )
    return list(reversed(rows))


# ========== 聊天主入口 ==========

async def _load_static_memory_text(agent_id: int) -> str:
    rows = await db.aquery(
        "SELECT content FROM static_memory WHERE agent_id=? ORDER BY id", (agent_id,)
    )
    return "\n".join(r["content"] for r in rows)


async def _load_history(session_id: int) -> tuple[list[dict], Optional[str]]:
    """窗口化历史：只取最近 RECENT_KEEP 条原文，更早的返回滚动摘要（若有）。"""
    summary_row = await db.aquery_one(
        "SELECT summary_text FROM session_summary WHERE session_id=?", (session_id,)
    )
    summary = summary_row["summary_text"] if summary_row else None
    rows = await db.aquery(
        "SELECT role, content FROM messages WHERE session_id=? ORDER BY id DESC LIMIT ?",
        (session_id, settings.RECENT_KEEP),
    )
    history = [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]
    return history, summary


async def _build_chat_context(session: dict, user: CurrentUser, user_input: str) -> dict:
    """按会话类型组装 LLM messages，返回上下文与调试信息。"""
    agent = await db.aquery_one("SELECT * FROM agents WHERE id=?", (session["agent_id"],))
    if not agent:
        raise HTTPException(404, "角色不存在或已被删除")

    static_mem_text = await _load_static_memory_text(session["agent_id"])
    history, summary = await _load_history(session["id"])

    # v2.1 融合：读取关系状态、近期情感、KV关键事实
    rel_row = await db.aquery_one(
        "SELECT * FROM relationship_state WHERE user_id=? AND agent_id=?",
        (user.id, session["agent_id"]),
    )
    relationship = (
        f"{rel_row['stage']}（亲密度 {rel_row['intimacy_score']}/100）"
        if rel_row else "初识（亲密度 0/100）"
    )
    emo_rows = await db.aquery(
        "SELECT sentiment, emotion_tag FROM emotional_log WHERE session_id=? ORDER BY id DESC LIMIT 5",
        (session["id"],),
    )
    emotion_context = ""
    if emo_rows:
        avg = sum(r["sentiment"] for r in emo_rows) / len(emo_rows)
        mood = "偏积极" if avg > 0.2 else ("偏消极" if avg < -0.2 else "平稳")
        tags = [r["emotion_tag"] for r in emo_rows if r["emotion_tag"]]
        emotion_context = f"近期情绪基调：{mood}（标签：{'/'.join(tags[-3:]) or '无'}）"
    kv_rows = await db.aquery(
        "SELECT key, value FROM kv_memory WHERE user_id=? AND agent_id=? ORDER BY updated_at DESC LIMIT 10",
        (user.id, session["agent_id"]),
    )
    kv_memories = "\n".join(f"- **{r['key']}**: {r['value']}" for r in kv_rows) if kv_rows else ""

    if session["session_type"] == "cakumi":
        rag_text = await run_in_threadpool(
            mem0_client.search, user_input, user.id, session["agent_id"], settings.RAG_TOP_K
        )
        messages = build_cakumi_messages(
            static_memories=static_mem_text,
            rag_memories=rag_text,
            agent_persona=agent["persona_prompt"] or "",
            summary=summary,
            history=history,
            user_input=user_input,
            kv_memories=kv_memories or None,
            relationship=relationship,
            emotion_context=emotion_context or None,
            # Mufy 式富角色卡注入
            appearance=agent.get("appearance") or None,
            sample_dialogue=agent.get("sample_dialogue") or None,
            author_note=agent.get("author_note") or None,
            sidebar_data=agent.get("sidebar_data") or None,
        )
    else:  # mufy
        timeline_rows = await db.aquery(
            "SELECT event_summary FROM story_timeline WHERE session_id=? ORDER BY id",
            (session["id"],),
        )
        timeline_text = "\n".join(f"- {r['event_summary']}" for r in timeline_rows)
        static_block = static_mem_text if session["inherit_static_memory"] else ""
        messages = build_mufy_messages(
            static_block=static_block,
            timeline_events=timeline_text,
            agent_persona=agent["persona_prompt"] or "",
            summary=summary,
            history=history,
            user_input=user_input,
            relationship=relationship,
            appearance=agent.get("appearance") or None,
            sample_dialogue=agent.get("sample_dialogue") or None,
        )

    prompt_used = "\n\n".join(f"[{m['role']}] {m['content']}" for m in messages)
    llm_row = await load_user_llm_row(user.id)
    return {"messages": messages, "prompt_used": prompt_used, "llm_row": llm_row}


async def _save_message(session_id: int, role: str, content: str) -> int:
    return await db.aexecute(
        "INSERT INTO messages(session_id,role,content) VALUES (?,?,?)", (session_id, role, content)
    )


def _sse(payload: dict) -> str:
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


async def _post_chat_tasks(session: dict, user: CurrentUser, user_input: str, reply: str) -> None:
    """每轮聊天后的后台任务：记忆入库 / 剧情抽取 / 摘要刷新 / 情感记录。"""
    try:
        if session["session_type"] == "cakumi":
            # Mem0 自动抽取细碎记忆（同步库，放线程池）
            await run_in_threadpool(
                mem0_client.add,
                f"用户消息：{user_input}\n角色回复：{reply}",
                user.id,
                session["agent_id"],
            )
        else:
            await _extract_story_event(session, user, user_input, reply)
        # v2.1 融合：记录情感日志 + 更新关系状态
        await _log_emotion(session, user, user_input, reply)
        await _refresh_summary_if_needed(session)
    except Exception:
        logger.exception("chat 后置任务失败 (session_id=%s)", session["id"])


@app.post("/chat")
async def chat_endpoint(
    req: ChatRequest,
    background_tasks: BackgroundTasks,
    user: CurrentUser = Depends(get_current_user),
):
    session = await get_owned_session(req.session_id, user)
    user_input = req.user_input.strip()
    if not user_input:
        raise HTTPException(400, "user_input 不能为空")

    ctx = await _build_chat_context(session, user, user_input)
    # 用户消息先落库：即使 LLM 失败也不丢用户输入
    await _save_message(session["id"], "user", user_input)

    if req.stream:
        return StreamingResponse(
            _chat_sse_stream(session, user, ctx, user_input),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    try:
        cfg = resolve_llm_config(ctx["llm_row"])
        reply = await llm_chat(ctx["messages"], cfg)
    except LLMError as e:
        logger.warning("LLM 调用失败: %s", e)
        raise HTTPException(502, str(e))
    assistant_msg_id = await _save_message(session["id"], "assistant", reply)
    background_tasks.add_task(_post_chat_tasks, session, user, user_input, reply)
    return {
        "reply": reply,
        "prompt_used": ctx["prompt_used"],
        "message_id": assistant_msg_id,
    }


async def _chat_sse_stream(session: dict, user: CurrentUser, ctx: dict, user_input: str):
    yield _sse(
        {
            "type": "meta",
            "session_id": session["id"],
            "session_type": session["session_type"],
            "prompt_used": ctx["prompt_used"],
        }
    )
    try:
        cfg = resolve_llm_config(ctx["llm_row"])
    except LLMError as e:
        yield _sse({"type": "error", "message": str(e)})
        return

    parts: list[str] = []
    try:
        async for delta in chat_stream(ctx["messages"], cfg):
            parts.append(delta)
            yield _sse({"type": "delta", "text": delta})
    except LLMError as e:
        logger.warning("SSE 流式调用失败: %s", e)
        yield _sse({"type": "error", "message": str(e)})
        return

    reply = "".join(parts)
    if not reply.strip():
        yield _sse({"type": "error", "message": "LLM 返回内容为空"})
        return
    # 流式路径没有 BackgroundTasks，直接在生成器内完成后置任务
    msg_id = await _save_message(session["id"], "assistant", reply)
    await _post_chat_tasks(session, user, user_input, reply)
    yield _sse({"type": "done", "reply": reply, "message_id": msg_id})


# ========== Mufy 剧情事件抽取 ==========

async def _extract_story_event(session: dict, user: CurrentUser, user_input: str, reply: str) -> None:
    try:
        cfg = resolve_llm_config(await load_user_llm_row(user.id))
        dialog = f"用户：{user_input}\n角色：{reply}"
        result = await chat_json(
            [{"role": "user", "content": EXTRACT_STORY_EVENT_PROMPT.format(dialog_content=dialog)}],
            cfg,
            temperature=0.2,
        )
        if not result or not result.get("need_save"):
            return
        summary = str(result.get("summary") or "").strip()
        if not summary:
            return

        # 可选：写入前校验是否违背静态人设
        if (
            settings.CONFLICT_CHECK_ENABLED
            and session["inherit_static_memory"]
            and await _conflicts_static_memory(session["agent_id"], summary, cfg)
        ):
            logger.info("剧情事件与静态人设冲突，已丢弃: %s", summary)
            return

        tags = result.get("tags") or []
        if not isinstance(tags, list):
            tags = []
        parent = result.get("parent_event_id")
        parent = parent if isinstance(parent, int) else None
        topic = str(result.get("topic") or "general")
        sentiment = float(result.get("sentiment") or 0.0)

        await db.aexecute(
            """INSERT INTO story_timeline
               (user_id, agent_id, session_id, event_summary, tags, causal_parent_id, branch_id, is_manual, sentiment, topic)
               VALUES (?,?,?,?,?,?,?,0,?,?)""",
            (
                user.id,
                session["agent_id"],
                session["id"],
                summary,
                json.dumps([str(t) for t in tags], ensure_ascii=False),
                parent,
                "main",
                sentiment,
                topic,
            ),
        )
        logger.info("新剧情事件已入时间线 (session_id=%s): %s", session["id"], summary)
    except LLMError as e:
        logger.warning("剧情事件抽取失败（LLM）: %s", e)
    except Exception:
        logger.exception("剧情事件抽取失败")


async def _conflicts_static_memory(agent_id: int, event_summary: str, cfg) -> bool:
    static_text = await _load_static_memory_text(agent_id)
    if not static_text.strip():
        return False
    prompt = CONFLICT_CHECK_PROMPT.format(static_memories=static_text, new_event=event_summary)
    result = await chat_json([{"role": "user", "content": prompt}], cfg, temperature=0.1)
    return bool(result and result.get("conflict"))


# ========== v2.1 融合：情感日志 + 关系状态（抄 HerAndHim） ==========

async def _log_emotion(session: dict, user: CurrentUser, user_input: str, reply: str) -> None:
    """每轮对话后异步分析情感，写入 emotional_log，并微调亲密度。"""
    try:
        cfg = resolve_llm_config(await load_user_llm_row(user.id))
        dialog = f"用户：{user_input}\n角色：{reply}"
        result = await chat_json(
            [{"role": "user", "content": EMOTION_ANALYSIS_PROMPT.format(dialog_content=dialog)}],
            cfg,
            temperature=0.1,
        )
        if not result:
            return
        sentiment = float(result.get("sentiment") or 0.0)
        emotion_tag = str(result.get("emotion") or "")
        await db.aexecute(
            "INSERT INTO emotional_log(session_id, sentiment, emotion_tag, note) VALUES (?,?,?,?)",
            (session["id"], sentiment, emotion_tag, dialog[:200]),
        )
        # 亲密度微调：正向情感 +1，负向 -1，范围 0-100
        delta = 1 if sentiment > 0.3 else (-1 if sentiment < -0.3 else 0)
        if delta:
            await db.aexecute(
                """INSERT INTO relationship_state(user_id, agent_id, intimacy_score, stage)
                   VALUES (?,?,?, '初识')
                   ON CONFLICT(user_id, agent_id) DO UPDATE SET
                     intimacy_score = MAX(0, MIN(100, intimacy_score + ?)),
                     updated_at = CURRENT_TIMESTAMP""",
                (user.id, session["agent_id"], delta, delta),
            )
    except Exception:
        logger.debug("情感分析失败（非致命）", exc_info=True)


# ========== 长对话滚动摘要 ==========

_summary_locks: dict[int, asyncio.Lock] = {}


async def _refresh_summary_if_needed(session: dict) -> None:
    """消息数超过阈值时，把窗口之外的旧消息增量压缩进 session_summary。"""
    try:
        row = await db.aquery_one(
            "SELECT COUNT(*) AS n FROM messages WHERE session_id=?", (session["id"],)
        )
        total = row["n"] if row else 0
        if total <= settings.SUMMARY_THRESHOLD:
            return

        recent = await db.aquery(
            "SELECT id FROM messages WHERE session_id=? ORDER BY id DESC LIMIT ?",
            (session["id"], settings.RECENT_KEEP),
        )
        target = recent[-1]["id"]  # 摘要应覆盖到「最早一条保留消息」之前
        summary_row = await db.aquery_one(
            "SELECT summary_text, covered_until_msg_id FROM session_summary WHERE session_id=?",
            (session["id"],),
        )
        covered = summary_row["covered_until_msg_id"] if summary_row else 0
        if covered >= target:
            return

        lock = _summary_locks.setdefault(session["id"], asyncio.Lock())
        async with lock:
            # 双重检查，避免并发聊天重复压缩
            summary_row = await db.aquery_one(
                "SELECT summary_text, covered_until_msg_id FROM session_summary WHERE session_id=?",
                (session["id"],),
            )
            covered = summary_row["covered_until_msg_id"] if summary_row else 0
            if covered >= target:
                return

            new_msgs = await db.aquery(
                "SELECT role, content FROM messages WHERE session_id=? AND id>? AND id<=? ORDER BY id",
                (session["id"], covered, target),
            )
            if not new_msgs:
                return
            dialog = "\n".join(
                f"{'用户' if m['role'] == 'user' else '角色'}: {m['content']}" for m in new_msgs
            )
            prior = summary_row["summary_text"] if summary_row else "（无）"
            cfg = resolve_llm_config(await load_user_llm_row(session["user_id"]))
            prompt = SUMMARY_PROMPT.format(prior_summary=prior, dialog=dialog)
            summary = await llm_chat([{"role": "user", "content": prompt}], cfg, temperature=0.3)

            if summary_row:
                await db.aexecute(
                    "UPDATE session_summary SET summary_text=?, covered_until_msg_id=?, updated_at=CURRENT_TIMESTAMP WHERE session_id=?",
                    (summary, target, session["id"]),
                )
            else:
                await db.aexecute(
                    "INSERT INTO session_summary(session_id, summary_text, covered_until_msg_id) VALUES (?,?,?)",
                    (session["id"], summary, target),
                )
            logger.info("会话 %s 滚动摘要已更新（覆盖至消息 %s）", session["id"], target)
    except LLMError as e:
        logger.warning("摘要刷新失败（LLM）: %s", e)
    except Exception:
        logger.exception("摘要刷新失败")


# ========== 静态记忆 CRUD（Cakumi 面板） ==========

@app.post("/static-memory/add")
async def add_static_memory(req: StaticMemoryAdd, user: CurrentUser = Depends(get_current_user)):
    await get_owned_agent(req.agent_id, user)
    memory_id = await db.aexecute(
        "INSERT INTO static_memory(user_id, agent_id, content, note) VALUES (?,?,?,?)",
        (user.id, req.agent_id, req.content, req.note),
    )
    return {"id": memory_id, "ok": True}


@app.get("/static-memory/list")
async def list_static_memory(agent_id: int, user: CurrentUser = Depends(get_current_user)):
    await get_owned_agent(agent_id, user)
    return await db.aquery(
        "SELECT * FROM static_memory WHERE agent_id=? ORDER BY id", (agent_id,)
    )


@app.patch("/static-memory/{memory_id}")
async def update_static_memory(
    memory_id: int, req: StaticMemoryUpdate, user: CurrentUser = Depends(get_current_user)
):
    row = await db.aquery_one("SELECT * FROM static_memory WHERE id=?", (memory_id,))
    if not row:
        raise HTTPException(404, "记忆不存在")
    await get_owned_agent(row["agent_id"], user)
    await db.aexecute(
        "UPDATE static_memory SET content=COALESCE(?, content), note=COALESCE(?, note) WHERE id=?",
        (req.content, req.note, memory_id),
    )
    return {"ok": True}


@app.delete("/static-memory/{memory_id}")
async def delete_static_memory(memory_id: int, user: CurrentUser = Depends(get_current_user)):
    row = await db.aquery_one("SELECT * FROM static_memory WHERE id=?", (memory_id,))
    if not row:
        raise HTTPException(404, "记忆不存在")
    await get_owned_agent(row["agent_id"], user)
    await db.aexecute("DELETE FROM static_memory WHERE id=?", (memory_id,))
    return {"ok": True}


# ========== 剧情时间线 CRUD（Mufy 面板） ==========

@app.post("/timeline/add-event")
async def add_timeline_event(req: TimelineEventAdd, user: CurrentUser = Depends(get_current_user)):
    session = await get_owned_session(req.session_id, user)
    event_id = await db.aexecute(
        """INSERT INTO story_timeline
           (user_id, agent_id, session_id, event_summary, tags, causal_parent_id, branch_id, is_manual)
           VALUES (?,?,?,?,?,?,?,1)""",
        (
            user.id,
            session["agent_id"],
            req.session_id,
            req.summary,
            json.dumps(req.tags, ensure_ascii=False),
            req.parent_id,
            req.branch_id,
        ),
    )
    return {"id": event_id}


@app.get("/timeline/list")
async def list_timeline(session_id: int, user: CurrentUser = Depends(get_current_user)):
    await get_owned_session(session_id, user)
    return await db.aquery(
        "SELECT * FROM story_timeline WHERE session_id=? ORDER BY id", (session_id,)
    )


@app.delete("/timeline/events/{event_id}")
async def delete_timeline_event(event_id: int, user: CurrentUser = Depends(get_current_user)):
    row = await db.aquery_one("SELECT * FROM story_timeline WHERE id=?", (event_id,))
    if not row:
        raise HTTPException(404, "剧情事件不存在")
    await get_owned_session(row["session_id"], user)
    await db.aexecute("DELETE FROM story_timeline WHERE id=?", (event_id,))
    return {"ok": True}


# ========== 用户 LLM 配置 ==========

@app.put("/llm-config")
async def upsert_llm_config(req: LLMConfigUpdate, user: CurrentUser = Depends(get_current_user)):
    existing = await load_user_llm_row(user.id)
    if existing:
        await db.aexecute(
            "UPDATE user_llm_config SET model_provider=?, model_name=?, api_key=?, base_url=? WHERE id=?",
            (req.provider, req.model, req.api_key, req.base_url, existing["id"]),
        )
        return {"ok": True, "id": existing["id"]}
    config_id = await db.aexecute(
        "INSERT INTO user_llm_config(user_id, model_provider, model_name, api_key, base_url) VALUES (?,?,?,?,?)",
        (user.id, req.provider, req.model, req.api_key, req.base_url),
    )
    return {"ok": True, "id": config_id}


@app.get("/llm-config")
async def get_llm_config(user: CurrentUser = Depends(get_current_user)):
    row = await load_user_llm_row(user.id)
    if not row:
        return {"configured": False}
    key = row.get("api_key") or ""
    masked = (key[:4] + "****" + key[-4:]) if len(key) >= 8 else "****"
    return {
        "configured": True,
        "provider": row.get("model_provider"),
        "model": row.get("model_name"),
        "base_url": row.get("base_url"),
        "api_key_masked": masked,
    }


# ========== v2.1 新增：KV 记忆 CRUD（HerAndHim consolidation） ==========

class KVMemoryAdd(BaseModel):
    agent_id: int
    key: str = Field(min_length=1, max_length=64)
    value: str = Field(min_length=1, max_length=1000)


@app.post("/kv-memory/add")
async def add_kv_memory(req: KVMemoryAdd, user: CurrentUser = Depends(get_current_user)):
    await get_owned_agent(req.agent_id, user)
    await db.aexecute(
        """INSERT INTO kv_memory(user_id, agent_id, key, value, source)
           VALUES (?,?,?,?, 'manual')
           ON CONFLICT(user_id, agent_id, key) DO UPDATE SET
             value=excluded.value, updated_at=CURRENT_TIMESTAMP""",
        (user.id, req.agent_id, req.key, req.value),
    )
    return {"ok": True}


@app.get("/kv-memory/list")
async def list_kv_memory(agent_id: int, user: CurrentUser = Depends(get_current_user)):
    await get_owned_agent(agent_id, user)
    return await db.aquery(
        "SELECT * FROM kv_memory WHERE user_id=? AND agent_id=? ORDER BY updated_at DESC",
        (user.id, agent_id),
    )


@app.delete("/kv-memory/{mem_id}")
async def delete_kv_memory(mem_id: int, user: CurrentUser = Depends(get_current_user)):
    row = await db.aquery_one("SELECT * FROM kv_memory WHERE id=?", (mem_id,))
    if not row:
        raise HTTPException(404, "记忆不存在")
    if row["user_id"] != user.id:
        raise HTTPException(403, "无权删除")
    await db.aexecute("DELETE FROM kv_memory WHERE id=?", (mem_id,))
    return {"ok": True}


# ========== v2.1 新增：关系状态查询/更新 ==========

@app.get("/relationship/{agent_id}")
async def get_relationship(agent_id: int, user: CurrentUser = Depends(get_current_user)):
    await get_owned_agent(agent_id, user)
    row = await db.aquery_one(
        "SELECT * FROM relationship_state WHERE user_id=? AND agent_id=?",
        (user.id, agent_id),
    )
    if not row:
        return {"intimacy_score": 0, "stage": "初识", "notes": ""}
    return row


class RelationshipUpdate(BaseModel):
    intimacy_delta: int = 0
    stage: Optional[str] = Field(default=None, max_length=32)


@app.patch("/relationship/{agent_id}")
async def update_relationship(
    agent_id: int, req: RelationshipUpdate, user: CurrentUser = Depends(get_current_user)
):
    await get_owned_agent(agent_id, user)
    await db.aexecute(
        "INSERT OR IGNORE INTO relationship_state(user_id, agent_id) VALUES (?,?)",
        (user.id, agent_id),
    )
    if req.intimacy_delta:
        await db.aexecute(
            """UPDATE relationship_state SET intimacy_score = MAX(0, MIN(100, intimacy_score + ?)),
               updated_at=CURRENT_TIMESTAMP WHERE user_id=? AND agent_id=?""",
            (req.intimacy_delta, user.id, agent_id),
        )
    if req.stage:
        await db.aexecute(
            "UPDATE relationship_state SET stage=?, updated_at=CURRENT_TIMESTAMP WHERE user_id=? AND agent_id=?",
            (req.stage, user.id, agent_id),
        )
    return {"ok": True}


# ========== Mufy 面具系统：玩家角色 CRUD ==========

class PlayerCharacterUpsert(BaseModel):
    name: Optional[str] = Field(default=None, max_length=64)
    gender: Optional[str] = Field(default=None, max_length=16)
    persona: Optional[str] = Field(default=None, max_length=2000)


@app.get("/player-character")
async def get_player_character(user: CurrentUser = Depends(get_current_user)):
    row = await db.aquery_one(
        "SELECT * FROM player_character WHERE user_id=?", (user.id,)
    )
    if not row:
        return {"configured": False}
    return {"configured": True, **row}


@app.put("/player-character")
async def upsert_player_character(
    req: PlayerCharacterUpsert, user: CurrentUser = Depends(get_current_user)
):
    await db.aexecute(
        """INSERT INTO player_character(user_id, name, gender, persona)
           VALUES (?,?,?,?,?)
           ON CONFLICT(user_id) DO UPDATE SET
             name=excluded.name, gender=excluded.gender, persona=excluded.persona,
             updated_at=CURRENT_TIMESTAMP""",
        (user.id, req.name, req.gender, req.persona),
    )
    return {"ok": True}


# ========== 健康检查 & 全局异常 ==========

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "mem0": "available" if mem0_client.is_available() else "degraded",
        "llm_env_key": bool(settings.LLM_API_KEY),
        "auth_enabled": settings.AUTH_ENABLED,
    }


@app.exception_handler(LLMError)
async def llm_error_handler(_, exc: LLMError):
    return JSONResponse(status_code=502, content={"error": str(exc)})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host=settings.HOST, port=settings.PORT, reload=True)

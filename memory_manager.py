"""
NexusCompanion 记忆管理器
融合三家设计：
- HerAndHim：key-value 长期记忆 + 写入时 consolidation 合并（ADD/UPDATE/DELETE/NOOP）+ 情感图谱 + 关系状态
- AstrBot：长对话滚动摘要
- Mem0：向量检索（动态 RAG 记忆）
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

# 系统保护键：LLM 不可篡改（抄 HerAndHim PROTECTED_KEYS）
PROTECTED_KEYS = {
    "bot_name", "user_name", "user_profile",
    "assistant_personality", "assistant_tone",
    "onboarding_completed",
}

# 对话摘要阈值：超过多少条消息触发压缩
SUMMARY_TRIGGER_MSG_COUNT = 20
# 保留最近多少条原文消息不压缩
KEEP_RECENT_MSGS = 10


class MemoryManager:
    """统一记忆入口：静态记忆 + KV动态记忆 + 关系状态 + 情感日志 + 时间线"""

    def __init__(self, db_path: str, mem=None):
        self.db_path = db_path
        self.mem = mem  # Mem0 实例，用于向量 RAG 检索

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    # ========== 静态显式记忆（Cakumi 最高优先级） ==========

    def get_static_memories(self, agent_id: int) -> str:
        """读取角色所有静态人设记忆，组装成 prompt 片段"""
        conn = self._conn()
        rows = conn.execute(
            "SELECT key, content FROM static_memory WHERE agent_id=? ORDER BY is_protected DESC, id",
            (agent_id,),
        ).fetchall()
        conn.close()
        if not rows:
            return "（暂无静态人设设定）"
        lines = []
        for r in rows:
            tag = "🔒" if r["key"] in PROTECTED_KEYS else "•"
            lines.append(f"{tag} [{r['key']}] {r['content']}")
        return "\n".join(lines)

    def add_static_memory(self, user_id: int, agent_id: int, key: str, content: str, is_protected: bool = False):
        conn = self._conn()
        conn.execute(
            """INSERT INTO static_memory(user_id,agent_id,key,content,is_protected)
               VALUES (?,?,?,?,?)
               ON CONFLICT(agent_id,key) DO UPDATE SET content=excluded.content, updated_at=CURRENT_TIMESTAMP""",
            (user_id, agent_id, key, content, is_protected),
        )
        conn.commit()
        conn.close()

    # ========== KV 动态记忆（HerAndHim consolidation 设计） ==========

    def get_kv_memories(self, user_id: int, agent_id: int) -> dict[str, str]:
        """返回该用户+角色的所有动态 key-value 记忆"""
        conn = self._conn()
        rows = conn.execute(
            "SELECT key, value FROM kv_memory WHERE user_id=? AND agent_id=?",
            (user_id, agent_id),
        ).fetchall()
        conn.close()
        return {r["key"]: r["value"] for r in rows}

    def remember(self, user_id: int, agent_id: int, key: str, value: str, source: str = "auto"):
        """存储一条动态记忆（upsert，最新值覆盖旧值）"""
        conn = self._conn()
        conn.execute(
            """INSERT INTO kv_memory(user_id,agent_id,key,value,source)
               VALUES (?,?,?,?,?)
               ON CONFLICT(user_id,agent_id,key) DO UPDATE SET value=excluded.value, source=excluded.source, updated_at=CURRENT_TIMESTAMP""",
            (user_id, agent_id, key, value, source),
        )
        conn.commit()
        conn.close()

    def forget(self, user_id: int, agent_id: int, key: str):
        conn = self._conn()
        conn.execute(
            "DELETE FROM kv_memory WHERE user_id=? AND agent_id=? AND key=?",
            (user_id, agent_id, key),
        )
        conn.commit()
        conn.close()

    # ========== 关系状态（HerAndHim RelationshipStore） ==========

    def get_relationship(self, user_id: int, agent_id: int) -> dict:
        conn = self._conn()
        row = conn.execute(
            "SELECT * FROM relationship_state WHERE user_id=? AND agent_id=?",
            (user_id, agent_id),
        ).fetchone()
        conn.close()
        if not row:
            return {"intimacy_score": 0, "stage": "初识", "notes": ""}
        return dict(row)

    def update_relationship(self, user_id: int, agent_id: int, intimacy_delta: int = 0, stage: Optional[str] = None):
        conn = self._conn()
        # 先确保有记录
        conn.execute(
            "INSERT OR IGNORE INTO relationship_state(user_id,agent_id) VALUES (?,?)",
            (user_id, agent_id),
        )
        if intimacy_delta:
            conn.execute(
                """UPDATE relationship_state SET intimacy_score = MAX(0, MIN(100, intimacy_score + ?)), updated_at=CURRENT_TIMESTAMP
                   WHERE user_id=? AND agent_id=?""",
                (intimacy_delta, user_id, agent_id),
            )
        if stage:
            conn.execute(
                "UPDATE relationship_state SET stage=?, updated_at=CURRENT_TIMESTAMP WHERE user_id=? AND agent_id=?",
                (stage, user_id, agent_id),
            )
        conn.commit()
        conn.close()

    # ========== 情感日志（HerAndHim EmotionalGraph） ==========

    def log_emotion(self, conversation_id: int, sentiment: float, emotion_tag: str = "", note: str = ""):
        conn = self._conn()
        conn.execute(
            "INSERT INTO emotional_log(conversation_id,sentiment,emotion_tag,note) VALUES (?,?,?,?)",
            (conversation_id, sentiment, emotion_tag, note),
        )
        conn.commit()
        conn.close()

    def get_recent_emotion_summary(self, conversation_id: int, days: int = 7) -> str:
        conn = self._conn()
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        rows = conn.execute(
            "SELECT sentiment, emotion_tag, note FROM emotional_log WHERE conversation_id=? AND created_at>=? ORDER BY created_at DESC LIMIT 5",
            (conversation_id, cutoff),
        ).fetchall()
        conn.close()
        if not rows:
            return ""
        avg_sentiment = sum(r["sentiment"] for r in rows) / len(rows)
        tags = [r["emotion_tag"] for r in rows if r["emotion_tag"]]
        tag_str = "/".join(tags[-3:]) if tags else ""
        mood = "偏积极" if avg_sentiment > 0.2 else ("偏消极" if avg_sentiment < -0.2 else "平稳")
        return f"近期情绪基调：{mood}（近{len(rows)}条对话，标签：{tag_str or '无'}）"

    # ========== 时间线事件（Mufy 时序剧情） ==========

    def add_timeline_event(self, user_id: int, agent_id: int, conversation_id: int,
                           summary: str, topic: str = "general", sentiment: float = 0.0,
                           keywords: list = None, causal_parent_id: int = None,
                           branch_id: int = None, is_manual: bool = False):
        conn = self._conn()
        conn.execute(
            """INSERT INTO story_timeline(user_id,agent_id,conversation_id,topic,event_summary,sentiment,keywords,causal_parent_id,branch_id,is_manual)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (user_id, agent_id, conversation_id, topic, summary, sentiment,
             json.dumps(keywords or [], ensure_ascii=False), causal_parent_id, branch_id, is_manual),
        )
        conn.commit()
        conn.close()

    def get_timeline_events(self, conversation_id: int, limit: int = 20) -> list[dict]:
        conn = self._conn()
        rows = conn.execute(
            "SELECT * FROM story_timeline WHERE conversation_id=? ORDER BY created_at ASC LIMIT ?",
            (conversation_id, limit),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    # ========== 长对话滚动摘要（AstrBot/Letta 思路） ==========

    def maybe_compress(self, conversation_id: int, llm_callable=None) -> str:
        """
        如果该对话的消息数超过阈值，调用 LLM 把较早的消息压缩成摘要。
        llm_callable: 接收 prompt 字符串，返回摘要文本。
        返回：当前应该注入 prompt 的历史文本（摘要 + 最近原文）。
        """
        conn = self._conn()
        total = conn.execute(
            "SELECT COUNT(*) as c FROM messages WHERE conversation_id=?", (conversation_id,)
        ).fetchone()["c"]

        # 已有摘要
        summary_row = conn.execute(
            "SELECT * FROM session_summary WHERE conversation_id=?", (conversation_id,)
        ).fetchone()

        if total < SUMMARY_TRIGGER_MSG_COUNT:
            # 不需要压缩，返回全部最近消息
            rows = conn.execute(
                "SELECT role, content FROM messages WHERE conversation_id=? ORDER BY id DESC LIMIT ?",
                (conversation_id, KEEP_RECENT_MSGS),
            ).fetchall()
            conn.close()
            history = "\n".join([f"{r['role']}: {r['content']}" for r in reversed(rows)])
            if summary_row:
                return f"【早期对话摘要】\n{summary_row['summary_text']}\n\n【最近对话】\n{history}"
            return history

        # 需要压缩：取出需要被摘要的旧消息
        covered_until = summary_row["covered_until_msg_id"] if summary_row else 0
        old_msgs = conn.execute(
            "SELECT role, content FROM messages WHERE conversation_id=? AND id>? ORDER BY id ASC",
            (conversation_id, covered_until),
        ).fetchall()

        if len(old_msgs) > KEEP_RECENT_MSGS and llm_callable:
            to_summarize = old_msgs[:-KEEP_RECENT_MSGS]
            newest_id = conn.execute(
                "SELECT id FROM messages WHERE conversation_id=? ORDER BY id DESC LIMIT ?",
                (conversation_id, KEEP_RECENT_MSGS),
            ).fetchall()[-1]["id"] if len(old_msgs) >= KEEP_RECENT_MSGS else covered_until

            old_text = "\n".join([f"{r['role']}: {r['content']}" for r in to_summarize])
            prompt = f"请把以下对话压缩成简洁摘要（保留关键事实、人物关系变化、重要约定，200字以内）：\n\n{old_text}"
            new_summary = llm_callable(prompt)

            if summary_row:
                # 合并旧摘要和新摘要
                combined = f"{summary_row['summary_text']}\n{new_summary}"
                conn.execute(
                    "UPDATE session_summary SET summary_text=?, covered_until_msg_id=?, updated_at=CURRENT_TIMESTAMP WHERE conversation_id=?",
                    (combined, newest_id, conversation_id),
                )
            else:
                conn.execute(
                    "INSERT INTO session_summary(conversation_id,summary_text,covered_until_msg_id) VALUES (?,?,?)",
                    (conversation_id, new_summary, newest_id),
                )
            conn.commit()
            covered_until = newest_id

        # 取最近原文
        recent_rows = conn.execute(
            "SELECT role, content FROM messages WHERE conversation_id=? AND id>? ORDER BY id ASC LIMIT ?",
            (conversation_id, covered_until, KEEP_RECENT_MSGS * 2),
        ).fetchall()
        conn.close()
        recent_text = "\n".join([f"{r['role']}: {r['content']}" for r in recent_rows])

        final_summary = conn.execute(
            "SELECT summary_text FROM session_summary WHERE conversation_id=?", (conversation_id,)
        ).fetchone()
        if final_summary:
            return f"【早期对话摘要】\n{final_summary['summary_text']}\n\n【最近对话】\n{recent_text}"
        return recent_text

    # ========== Boot 上下文（HerAndHim boot_context） ==========

    def build_boot_context(self, user_id: int, agent_id: int, conversation_id: int) -> str:
        """会话启动时自动注入的上下文快照"""
        parts = []

        # 1. 静态人设
        static = self.get_static_memories(agent_id)
        if static:
            parts.append(f"【角色固定人设】\n{static}")

        # 2. KV 动态记忆
        kv = self.get_kv_memories(user_id, agent_id)
        if kv:
            kv_lines = [f"- **{k}**: {v}" for k, v in list(kv.items())[:15]]
            parts.append("【过往记忆】\n" + "\n".join(kv_lines))

        # 3. 关系状态
        rel = self.get_relationship(user_id, agent_id)
        parts.append(f"【关系状态】{rel['stage']}（亲密度 {rel['intimacy_score']}/100）")

        # 4. 近期情绪
        emo = self.get_recent_emotion_summary(conversation_id)
        if emo:
            parts.append(emo)

        return "\n\n".join(parts)

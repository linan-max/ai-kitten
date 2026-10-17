"""SQLite 访问层。

设计要点：
- 每次操作独立开连接（WAL 模式），避免跨线程共享连接带来的隐患；
- 同步原子操作通过 run_in_threadpool 包装为 async，FastAPI 端点里直接 await；
- 启动时执行 schema.sql 自动建表（幂等，使用 CREATE TABLE IF NOT EXISTS）。
"""
from __future__ import annotations

import sqlite3
from typing import Any, Iterable, Optional

from starlette.concurrency import run_in_threadpool

from config import settings


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA journal_mode = {settings.SQLITE_JOURNAL_MODE}")
    return conn


def init_db() -> None:
    """建表 / 初始化（幂等）。journal 模式在每个连接上设置，无需额外处理。"""
    conn = _connect()
    try:
        conn.executescript(settings.SCHEMA_PATH.read_text(encoding="utf-8"))
        conn.commit()
    finally:
        conn.close()


# ---------------- 同步原子操作（线程池内执行） ----------------

def _query(sql: str, params: Iterable[Any]) -> list[dict]:
    conn = _connect()
    try:
        cur = conn.execute(sql, tuple(params))
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def _query_one(sql: str, params: Iterable[Any]) -> Optional[dict]:
    rows = _query(sql, params)
    return rows[0] if rows else None


def _execute(sql: str, params: Iterable[Any]) -> int:
    """执行单条写语句，返回 lastrowid。"""
    conn = _connect()
    try:
        cur = conn.execute(sql, tuple(params))
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def _execute_many(statements: list[tuple[str, Iterable[Any]]]) -> None:
    """在一个事务里执行多条写语句（任一条失败整体回滚）。"""
    conn = _connect()
    try:
        for sql, params in statements:
            conn.execute(sql, tuple(params))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------- async 包装（FastAPI 端点中直接 await） ----------------

async def aquery(sql: str, params: Optional[Iterable[Any]] = None) -> list[dict]:
    return await run_in_threadpool(_query, sql, params or ())


async def aquery_one(sql: str, params: Optional[Iterable[Any]] = None) -> Optional[dict]:
    return await run_in_threadpool(_query_one, sql, params or ())


async def aexecute(sql: str, params: Optional[Iterable[Any]] = None) -> int:
    return await run_in_threadpool(_execute, sql, params or ())


async def aexecute_many(statements: list[tuple[str, Iterable[Any]]]) -> None:
    await run_in_threadpool(_execute_many, statements)

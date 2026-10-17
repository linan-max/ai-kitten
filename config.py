"""全局配置：全部支持环境变量覆盖（可用 .env 文件），便于本地/部署切换。"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except (TypeError, ValueError):
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "").strip() or default)
    except (TypeError, ValueError):
        return default


class Settings:
    # ---- 服务 ----
    APP_NAME: str = "NexusCompanion"
    HOST: str = os.getenv("NEXUS_HOST", "127.0.0.1")
    PORT: int = _int("NEXUS_PORT", 8000)

    # ---- 安全 ----
    # 生产环境必须通过 NEXUS_SECRET_KEY 覆盖，否则重启后旧 Token 全部失效
    SECRET_KEY: str = os.getenv("NEXUS_SECRET_KEY", "dev-secret-please-change-me")
    TOKEN_EXPIRE_MINUTES: int = _int("NEXUS_TOKEN_EXPIRE_MINUTES", 60 * 24 * 7)
    # 原型开发可关闭认证（默认走第一个用户），生产务必开启
    AUTH_ENABLED: bool = _bool("NEXUS_AUTH_ENABLED", True)

    # ---- 数据库 / 向量库 ----
    DB_PATH: str = os.getenv("NEXUS_DB_PATH", str(BASE_DIR / "sqlite.db"))
    SCHEMA_PATH: Path = BASE_DIR / "schema.sql"
    CHROMA_PATH: str = os.getenv("NEXUS_CHROMA_PATH", str(BASE_DIR / "chroma_db"))
    # WAL 并发最好；MEMORY/OFF 用于禁止文件删除的受限环境（如部分沙箱）
    SQLITE_JOURNAL_MODE: str = os.getenv("NEXUS_SQLITE_JOURNAL_MODE", "WAL").upper()

    # ---- LLM 网关默认配置（用户可在 user_llm_config 中覆盖）----
    LLM_API_KEY: str = os.getenv("NEXUS_LLM_API_KEY", "")
    LLM_BASE_URL: str = os.getenv("NEXUS_LLM_BASE_URL", "https://api.deepseek.com/v1")
    LLM_MODEL: str = os.getenv("NEXUS_LLM_MODEL", "deepseek-chat")
    LLM_TEMPERATURE: float = _float("NEXUS_LLM_TEMPERATURE", 0.8)
    LLM_MAX_TOKENS: int = _int("NEXUS_LLM_MAX_TOKENS", 1024)

    # ---- 上下文管理 ----
    # 只把最近 N 条消息以原文进入 Prompt，更早的由滚动摘要承载
    RECENT_KEEP: int = _int("NEXUS_RECENT_KEEP", 16)
    # 消息总数超过该值才触发摘要压缩（ summarized 在后台异步刷新）
    SUMMARY_THRESHOLD: int = _int("NEXUS_SUMMARY_THRESHOLD", 30)
    RAG_TOP_K: int = _int("NEXUS_RAG_TOP_K", 5)

    # ---- 功能开关 ----
    MEM0_ENABLED: bool = _bool("NEXUS_MEM0_ENABLED", True)
    # Mufy 自动写时间线前是否校验与静态人设的冲突（多一次 LLM 调用）
    CONFLICT_CHECK_ENABLED: bool = _bool("NEXUS_CONFLICT_CHECK_ENABLED", False)


settings = Settings()

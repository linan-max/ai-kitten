"""Mem0 动态记忆封装（仅 Cakumi 会话使用）。

- 未安装 mem0ai / 开关关闭 / 未配置 LLM Key 时自动降级：
  检索返回占位文本「（暂无相关记忆）」，写入静默跳过，主流程不受影响。
"""
from __future__ import annotations

import logging
from typing import Optional

from config import settings

logger = logging.getLogger("nexus.mem0")

try:
    from mem0 import Memory
except Exception:  # pragma: no cover - 依赖缺失时走降级逻辑
    Memory = None  # type: ignore

_mem: Optional["Memory"] = None
_unavailable_reason: Optional[str] = None


def _init_memory() -> Optional["Memory"]:
    global _mem, _unavailable_reason
    if _mem is not None:
        return _mem
    if _unavailable_reason is not None:
        return None

    if not settings.MEM0_ENABLED:
        _unavailable_reason = "NEXUS_MEM0_ENABLED=false"
    elif Memory is None:
        _unavailable_reason = "未安装 mem0ai"
    elif not settings.LLM_API_KEY:
        _unavailable_reason = "未配置 NEXUS_LLM_API_KEY"
    else:
        try:
            _mem = Memory(
                config={
                    "vector_store": {
                        "provider": "chroma",
                        "config": {"path": settings.CHROMA_PATH},
                    },
                    "llm": {
                        "provider": "openai",
                        "config": {
                            "model": settings.LLM_MODEL,
                            "api_key": settings.LLM_API_KEY,
                            "openai_base_url": settings.LLM_BASE_URL,
                        },
                    },
                }
            )
        except Exception as e:
            _unavailable_reason = f"Mem0 初始化失败: {e}"
            logger.warning("Mem0 初始化失败，动态记忆降级: %s", e)

    if _mem is None:
        logger.warning(
            "Mem0 不可用（%s），Cakumi RAG 记忆降级为占位文本", _unavailable_reason
        )
    return _mem


def is_available() -> bool:
    return _init_memory() is not None


def search(query: str, user_id: int, agent_id: int, limit: int = 5) -> str:
    mem = _init_memory()
    if mem is None:
        return "（暂无相关记忆）"
    try:
        results = mem.search(
            query=query, user_id=str(user_id), agent_id=str(agent_id), limit=limit
        )
        lines = [item["memory"] for item in results if item.get("memory")]
        return "\n".join(lines) if lines else "（暂无相关记忆）"
    except Exception as e:
        logger.warning("Mem0 检索失败: %s", e)
        return "（暂无相关记忆）"


def add(text: str, user_id: int, agent_id: int) -> None:
    mem = _init_memory()
    if mem is None:
        return
    try:
        mem.add(text, user_id=str(user_id), agent_id=str(agent_id))
    except Exception as e:
        logger.warning("Mem0 写入失败: %s", e)

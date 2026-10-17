"""LLM 网关：统一封装 OpenAI 兼容协议。

- 支持 DeepSeek / OpenAI / Ollama / vLLM / OneAPI 等一切 OpenAI 兼容端点；
- 每个用户可在 user_llm_config 中配置自己的 Key，未配置时回落到环境变量默认值；
- 提供三种能力：普通对话 / 流式对话 / JSON 抽取（带围栏与噪声剥离的健壮解析）。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import AsyncIterator, Optional

from openai import AsyncOpenAI

from config import settings


class LLMError(RuntimeError):
    """LLM 调用失败（未配置 Key / 网络错误 / 返回为空等）。"""


@dataclass
class LLMConfig:
    model: str
    api_key: str
    base_url: str
    provider: str = "openai-compatible"


_clients: dict[tuple[str, str], AsyncOpenAI] = {}


def _client(cfg: LLMConfig) -> AsyncOpenAI:
    key = (cfg.base_url, cfg.api_key)
    client = _clients.get(key)
    if client is None:
        client = AsyncOpenAI(api_key=cfg.api_key or "sk-empty", base_url=cfg.base_url)
        _clients[key] = client
    return client


def resolve_llm_config(row: Optional[dict]) -> LLMConfig:
    """优先使用用户自定义配置（user_llm_config），否则回落环境变量，再否则报错。"""
    if row and row.get("api_key"):
        return LLMConfig(
            model=row.get("model_name") or settings.LLM_MODEL,
            api_key=row["api_key"],
            base_url=row.get("base_url") or settings.LLM_BASE_URL,
            provider=row.get("model_provider") or "openai-compatible",
        )
    if settings.LLM_API_KEY:
        return LLMConfig(
            model=settings.LLM_MODEL,
            api_key=settings.LLM_API_KEY,
            base_url=settings.LLM_BASE_URL,
            provider="env",
        )
    raise LLMError(
        "尚未配置 LLM：请调用 PUT /llm-config 为该用户配置 API Key，"
        "或设置环境变量 NEXUS_LLM_API_KEY / NEXUS_LLM_BASE_URL / NEXUS_LLM_MODEL"
    )


async def chat(
    messages: list[dict],
    cfg: LLMConfig,
    *,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
) -> str:
    try:
        resp = await _client(cfg).chat.completions.create(
            model=cfg.model,
            messages=messages,
            temperature=settings.LLM_TEMPERATURE if temperature is None else temperature,
            max_tokens=settings.LLM_MAX_TOKENS if max_tokens is None else max_tokens,
        )
    except Exception as e:
        raise LLMError(f"{type(e).__name__}: {e}") from e
    content = resp.choices[0].message.content if resp.choices else None
    if not content or not content.strip():
        raise LLMError("LLM 返回内容为空")
    return content


async def chat_stream(messages: list[dict], cfg: LLMConfig) -> AsyncIterator[str]:
    """流式对话：逐段 yield 增量文本。"""
    try:
        stream = await _client(cfg).chat.completions.create(
            model=cfg.model,
            messages=messages,
            temperature=settings.LLM_TEMPERATURE,
            max_tokens=settings.LLM_MAX_TOKENS,
            stream=True,
        )
        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta.content
            if delta:
                yield delta
    except Exception as e:
        raise LLMError(f"{type(e).__name__}: {e}") from e


def extract_json(text: str) -> Optional[dict]:
    """从 LLM 输出中健壮地解析 JSON 对象（兼容 ```json 围栏与前后噪声）。"""
    text = (text or "").strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch == "{":
            try:
                obj, _ = decoder.raw_decode(text[i:])
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                continue
    return None


async def chat_json(
    messages: list[dict],
    cfg: LLMConfig,
    *,
    temperature: Optional[float] = None,
) -> Optional[dict]:
    """调用 LLM 并解析为 JSON，失败返回 None（由调用方决定降级行为）。"""
    text = await chat(messages, cfg, temperature=temperature)
    return extract_json(text)

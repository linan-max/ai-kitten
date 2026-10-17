"""NexusCompanion Prompt 构建模块 v2.1

融合：
- Cakumi：静态人设最高优先级 + Mem0 RAG 动态记忆 + KV关键事实
- HerAndHim：关系状态/情感上下文注入（可选参数，向后兼容）
- Mufy：时序剧情时间线 + 分支叙事

所有模板产出 OpenAI messages 数组（system + 历史 + 用户输入）。
"""
from __future__ import annotations

from typing import Optional


def build_cakumi_messages(
    *,
    static_memories: str,
    rag_memories: str,
    agent_persona: str,
    summary: Optional[str],
    history: list[dict],
    user_input: str,
    # v2.1 新增可选参数（向后兼容，不传则不注入）
    kv_memories: Optional[str] = None,
    relationship: Optional[str] = None,
    emotion_context: Optional[str] = None,
    # Mufy 式富角色卡字段
    appearance: Optional[str] = None,
    sample_dialogue: Optional[str] = None,
    author_note: Optional[str] = None,
    sidebar_data: Optional[str] = None,
) -> list[dict]:
    """Cakumi 人设锁会话：静态人设 + Mem0 RAG + 可选关系/情感/KV记忆/小剧场。"""
    parts = [
        "你是用户的 AI 伴侣角色，请完全代入以下人设与用户对话。",
        "",
        "【角色固定人设 · 最高优先级，任何情况下不得违背】",
        static_memories or "（暂无）",
    ]
    if appearance:
        parts += ["", "【角色外观】", appearance]
    parts += [
        "",
        "【角色基础人设描述】",
        agent_persona or "（暂无）",
    ]
    if author_note:
        parts += ["", "【作者设定备注】", author_note]
    if sidebar_data:
        parts += ["", "【角色属性面板】", sidebar_data]
    if sample_dialogue:
        parts += ["", "【角色说话风格示例（模仿这个语气和节奏）】", sample_dialogue]
    if relationship:
        parts += ["", "【当前关系状态】", relationship]
    if emotion_context:
        parts += ["", "【近期情感基调】", emotion_context]
    parts += [
        "",
        "【过往重要记忆（自动检索，可在回复中自然呼应，不要生硬罗列）】",
        rag_memories or "（暂无相关记忆）",
    ]
    if kv_memories:
        parts += ["", "【关键事实记忆】", kv_memories]
    if summary:
        parts += [
            "",
            "【早期对话摘要（更早的聊天已压缩，仅供理解上下文）】",
            summary,
        ]
    system = "\n".join(parts)
    return [
        {"role": "system", "content": system},
        *history,
        {"role": "user", "content": user_input},
    ]


def build_mufy_messages(
    *,
    static_block: str,
    timeline_events: str,
    agent_persona: str,
    summary: Optional[str],
    history: list[dict],
    user_input: str,
    # v2.1 新增可选参数
    relationship: Optional[str] = None,
    # Mufy 式富角色卡字段
    appearance: Optional[str] = None,
    sample_dialogue: Optional[str] = None,
) -> list[dict]:
    """Mufy 长篇叙事会话：剧情时间线驱动，静态人设可选继承。"""
    parts = ["你正在陪伴用户进行一段长篇叙事扮演，请以角色身份沉浸式续写故事。"]
    if static_block.strip():
        parts += ["", "【角色基础设定（继承静态人设，与剧情冲突时以此为准）】", static_block]
    if appearance:
        parts += ["", "【角色外观】", appearance]
    if sample_dialogue:
        parts += ["", "【角色说话风格示例（模仿这个语气）】", sample_dialogue]
    if relationship:
        parts += ["", "【当前关系状态】", relationship]
    parts += [
        "",
        "【故事时间线（按发生先后排序，最新在末尾；续写时不得与已发生事件矛盾）】",
        timeline_events or "（暂无剧情事件）",
        "",
        "【角色基础人设描述】",
        agent_persona or "（暂无）",
    ]
    if summary:
        parts += ["", "【早期对话摘要】", summary]
    parts += [
        "",
        "【叙事要求】",
        "- 聚焦当前场景、动作、对话与心理，推进剧情而非复述时间线；",
        "- 保持角色口吻与人设一致，允许人物状态随剧情变化；",
        "- 自然地留下可被记录的剧情节点（关系变化、约定、转折）。",
    ]
    system = "\n".join(parts)
    return [
        {"role": "system", "content": system},
        *history,
        {"role": "user", "content": user_input},
    ]


# ========== 后台任务 Prompt ==========

EXTRACT_STORY_EVENT_PROMPT = """
阅读下面对话，判断本轮对话是否产生【关键剧情事件】。
关键事件定义：人物关系变化、场景切换、重大约定、重要行动、剧情转折。
普通闲聊、简单情绪安抚不要生成事件。

严格输出 JSON，不要输出任何额外解释：
{{
    "need_save": true 或 false,
    "topic": "话题标签，如 情感/场景/约定",
    "summary": "事件简述，一句话",
    "sentiment": 0.0,
    "tags": ["标签1", "标签2"]
}}

对话内容：
{dialog_content}
"""

CONFLICT_CHECK_PROMPT = """
下面是角色的静态人设设定，和一段新生成的剧情事件。
判断该剧情是否违背静态人设（如：静态设定角色怕水，剧情却写角色主动下水）。
若冲突，输出 JSON：{{"conflict": true, "reason": "原因"}}；
若不冲突，输出：{{"conflict": false, "reason": ""}}

静态人设：
{static_memories}

新剧情事件：
{new_event}
"""

SUMMARY_PROMPT = """
你在维护一份长期对话摘要。请把【已有摘要】和【新增对话】融合成一份更新后的完整摘要。

要求：
1. 保留：人物关系变化、关键约定、重要场景、未解决的悬念；
2. 删除：寒暄、重复、与主线无关的内容；
3. 控制在 300 字以内；
4. 直接输出摘要正文，不要任何前后缀解释。

【已有摘要】
{prior_summary}

【新增对话】
{dialog}
"""

# v2.1 新增：记忆合并/去重（HerAndHim consolidation）
MEMORY_CONSOLIDATION_PROMPT = """
你在维护 AI 伴侣的长期记忆库。新事实即将存入，对比已有相似记忆，决定唯一操作：
- ADD：全新信息，直接存储（可自己起一个更简洁的 key）
- UPDATE：新事实修正/覆盖了旧记忆，返回旧 key + 合并后的最新值
- DELETE：新事实使旧记忆失效，返回要删除的旧 key
- NOOP：已经知道了，不改变

【新事实】
key: {new_key}
value: {new_value}

【已有相似记忆】
{existing_memories}

只输出 JSON，不要解释：
{{"op": "ADD|UPDATE|DELETE|NOOP", "key": "...", "value": "..."}}
"""

# v2.1 新增：情感分析
EMOTION_ANALYSIS_PROMPT = """
分析下面这段对话中角色的情感状态。
输出 JSON：{{"sentiment": -1.0到1.0的数字, "emotion": "开心/难过/生气/温柔/平静/..."}}
对话：
{dialog_content}
"""

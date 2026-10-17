-- NexusCompanion 数据库建表脚本 v2.1
-- 增量融合 HerAndHim（KV记忆合并 + 关系状态 + 情感日志）+ Mufy（时序剧情）+ AstrBot（对话摘要）
-- 兼容 v1 表结构，新增的表不会破坏现有 main.py

-- 用户表
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 用户 LLM 配置（用户自定义 API Key，OpenAI 兼容协议）
CREATE TABLE IF NOT EXISTS user_llm_config (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    model_provider TEXT,
    model_name TEXT,
    api_key TEXT,
    base_url TEXT,
    FOREIGN KEY(user_id) REFERENCES users(id)
);

-- 角色卡表（融合 Mufy 富角色卡：外观/作者有话说/小剧场/侧边栏/标签/性别）
CREATE TABLE IF NOT EXISTS agents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    avatar TEXT,
    persona_prompt TEXT,
    opening_message TEXT,
    -- Mufy 式扩展字段
    gender TEXT,                     -- 角色性别
    appearance TEXT,                 -- 外观描述（发型/五官/服装/身高体型）
    author_note TEXT,                -- 作者有话说
    sample_dialogue TEXT,            -- 小剧场（示例对话，给 LLM 学语气）
    sidebar_data TEXT,              -- 侧边栏 JSON（结构化属性：身份/能力/状态）
    tags TEXT,                       -- JSON 数组标签 ["古风","纯爱"]
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(user_id) REFERENCES users(id)
);

-- 玩家角色（面具系统）：玩家在故事里扮演的身份
CREATE TABLE IF NOT EXISTS player_character (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    name TEXT,
    gender TEXT,
    persona TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(user_id) REFERENCES users(id),
    UNIQUE(user_id)
);

-- 会话表，session_type 区分两种模式：cakumi / mufy（创建后不可修改）
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    agent_id INTEGER NOT NULL,
    session_name TEXT,
    session_type TEXT NOT NULL DEFAULT 'cakumi',
    inherit_static_memory BOOLEAN DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(user_id) REFERENCES users(id),
    FOREIGN KEY(agent_id) REFERENCES agents(id)
);

-- 聊天消息表（按 id 即时间顺序）
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(session_id) REFERENCES sessions(id)
);

-- Cakumi 静态显式人设记忆，绑定角色 agent_id，该角色所有会话共享
CREATE TABLE IF NOT EXISTS static_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    agent_id INTEGER NOT NULL,
    content TEXT NOT NULL,
    note TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(user_id) REFERENCES users(id),
    FOREIGN KEY(agent_id) REFERENCES agents(id)
);

-- Mufy 时序剧情事件表
CREATE TABLE IF NOT EXISTS story_timeline (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    agent_id INTEGER NOT NULL,
    session_id INTEGER,
    event_summary TEXT NOT NULL,
    tags TEXT,
    causal_parent_id INTEGER,
    branch_id TEXT NOT NULL DEFAULT 'main',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_manual BOOLEAN DEFAULT 0,
    -- v2.1 新增：情感分 + 话题（融合 HerAndHim TemporalMemoryIndex）
    sentiment REAL NOT NULL DEFAULT 0.0,
    topic TEXT NOT NULL DEFAULT 'general',
    FOREIGN KEY(user_id) REFERENCES users(id),
    FOREIGN KEY(agent_id) REFERENCES agents(id),
    FOREIGN KEY(session_id) REFERENCES sessions(id)
);

-- 长对话滚动摘要（两模式通用）
CREATE TABLE IF NOT EXISTS session_summary (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL UNIQUE,
    summary_text TEXT NOT NULL,
    covered_until_msg_id INTEGER NOT NULL DEFAULT 0,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(session_id) REFERENCES sessions(id)
);

-- ========== v2.1 新增：融合 HerAndHim 的三层记忆增强 ==========

-- KV 动态记忆（HerAndHim consolidation：最新值覆盖旧值，自动合并去重）
CREATE TABLE IF NOT EXISTS kv_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    agent_id INTEGER NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'auto',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(user_id) REFERENCES users(id),
    FOREIGN KEY(agent_id) REFERENCES agents(id),
    UNIQUE(user_id, agent_id, key)
);

-- 关系状态（HerAndHim RelationshipStore：亲密度 + 关系阶段）
CREATE TABLE IF NOT EXISTS relationship_state (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    agent_id INTEGER NOT NULL,
    intimacy_score INTEGER NOT NULL DEFAULT 0,
    stage TEXT NOT NULL DEFAULT '初识',
    notes TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(user_id) REFERENCES users(id),
    FOREIGN KEY(agent_id) REFERENCES agents(id),
    UNIQUE(user_id, agent_id)
);

-- 情感日志（HerAndHim EmotionalGraph：时间序列情感，用于上下文注入）
CREATE TABLE IF NOT EXISTS emotional_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    sentiment REAL NOT NULL DEFAULT 0.0,
    emotion_tag TEXT,
    note TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(session_id) REFERENCES sessions(id)
);

-- 索引
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
CREATE INDEX IF NOT EXISTS idx_static_memory_agent ON static_memory(agent_id);
CREATE INDEX IF NOT EXISTS idx_timeline_session ON story_timeline(session_id);
CREATE INDEX IF NOT EXISTS idx_timeline_branch ON story_timeline(session_id, branch_id);
CREATE INDEX IF NOT EXISTS idx_user_llm_config_user ON user_llm_config(user_id);
CREATE INDEX IF NOT EXISTS idx_kv_memory_lookup ON kv_memory(user_id, agent_id, key);
CREATE INDEX IF NOT EXISTS idx_emotional_session ON emotional_log(session_id);

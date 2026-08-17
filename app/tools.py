from __future__ import annotations

from typing import Any

from .runtime import Tool, ToolRegistry


def build_novel_tools(db, pid: str) -> ToolRegistry:
    """Read-only novel tools available to specialist agents.

    The registry intentionally exposes domain concepts instead of generic SQL/filesystem access.
    This keeps the agent autonomous inside the novel while preserving the product's source of truth.
    """

    registry = ToolRegistry()

    def register(name: str, description: str, params: dict[str, Any], handler):
        registry.register(Tool(name=name, description=description, parameters=params, handler=handler, permission="read"))

    register(
        "project_status",
        "读取作品基础信息、当前章节数和主要状态概览。",
        {"type": "object", "properties": {}, "additionalProperties": False},
        lambda: {**db.get_project(pid), "dashboard": db.project_dashboard(pid), "preferences": db.get_preferences(pid)},
    )
    register(
        "chapter",
        "读取指定章节正文与章节元数据。只有需要核对具体历史事实时使用。",
        {
            "type": "object",
            "properties": {"chapter": {"type": "integer", "minimum": 1, "maximum": 100000}},
            "required": ["chapter"],
            "additionalProperties": False,
        },
        lambda chapter: db.chapter_by_ordinal(pid, chapter) or {"missing": True, "chapter": chapter},
    )
    register(
        "chapters_range",
        "读取一个很小的连续章节区间，用于核对因果与场景承接。",
        {
            "type": "object",
            "properties": {
                "start": {"type": "integer", "minimum": 1, "maximum": 100000},
                "end": {"type": "integer", "minimum": 1, "maximum": 100000},
            },
            "required": ["start", "end"],
            "additionalProperties": False,
        },
        lambda start, end: db.chapters_range(pid, max(1, start), min(max(start, end), start + 12)),
    )
    register(
        "search_history",
        "用中文自然语言检索旧章节和长期摘要。查人物曾经说过什么、物品来历、旧事件时优先使用。",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 500},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        lambda query, limit=8: db.search_memory(pid, query, min(limit, 20)),
    )
    register(
        "search_facts",
        "检索当前有效设定事实，例如身份、规则、物品状态、组织关系、时间规则。",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 500},
                "limit": {"type": "integer", "minimum": 1, "maximum": 40},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        lambda query, limit=20: db.search_canon(pid, query, min(limit, 40)),
    )
    register(
        "character",
        "按人物名检索最新人物状态：地点、目标、知识、关系、资源、伤势和秘密。",
        {
            "type": "object",
            "properties": {"name": {"type": "string", "minLength": 1, "maxLength": 120}},
            "required": ["name"],
            "additionalProperties": False,
        },
        lambda name: db.search_entities(pid, name, 12),
    )
    register(
        "open_threads",
        "读取开放伏笔、悬念、关系承诺和计划兑现位置。",
        {
            "type": "object",
            "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 100}},
            "additionalProperties": False,
        },
        lambda limit=40: db.open_threads(pid, min(limit, 100)),
    )
    register(
        "thread_priorities",
        "读取当前伏笔债务：已到期、即将到期、长期未推进和当前开放线索。",
        {"type": "object", "properties": {}, "additionalProperties": False},
        lambda: db.thread_scheduler_state(pid, 100),
    )
    register(
        "story_summaries",
        "读取不同尺度的长期剧情摘要，适合先建立大局，再决定是否下钻旧章节。",
        {
            "type": "object",
            "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 50}},
            "additionalProperties": False,
        },
        lambda limit=24: db.summaries(pid, min(limit, 50)),
    )
    register(
        "recent_patterns",
        "读取最近章节的剧情指纹，用于判断冲突、场景、情绪、反转和解决方式是否重复。",
        {
            "type": "object",
            "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 80}},
            "additionalProperties": False,
        },
        lambda limit=40: db.recent_signatures(pid, min(limit, 80)),
    )
    return registry

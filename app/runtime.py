from __future__ import annotations

import inspect
import json
import math
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .config import settings

ToolHandler = Callable[..., Any] | Callable[..., Awaitable[Any]]
EventHook = Callable[[int, str, str, dict[str, Any]], Awaitable[None]]


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler
    permission: str = "read"

    def api_schema(self) -> dict[str, Any]:
        return {"type": "function", "function": {"name": self.name, "description": self.description, "parameters": self.parameters}}


@dataclass
class ToolRegistry:
    tools: dict[str, Tool] = field(default_factory=dict)

    def register(self, tool: Tool) -> None:
        if tool.name in self.tools:
            raise ValueError(f"duplicate tool: {tool.name}")
        self.tools[tool.name] = tool

    def schemas(self, allowed_permissions: set[str] | None = None) -> list[dict[str, Any]]:
        allowed = allowed_permissions or {"read"}
        return [t.api_schema() for t in self.tools.values() if t.permission in allowed]

    @staticmethod
    def _validate_value(value: Any, schema: dict[str, Any], path: str) -> None:
        expected = schema.get("type")
        if expected == "integer":
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{path} must be an integer")
            if "minimum" in schema and value < schema["minimum"]:
                raise ValueError(f"{path} must be >= {schema['minimum']}")
            if "maximum" in schema and value > schema["maximum"]:
                raise ValueError(f"{path} must be <= {schema['maximum']}")
        elif expected == "number":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{path} must be a number")
            if not math.isfinite(float(value)):
                raise ValueError(f"{path} must be a finite number")
            if "minimum" in schema and value < schema["minimum"]:
                raise ValueError(f"{path} must be >= {schema['minimum']}")
            if "maximum" in schema and value > schema["maximum"]:
                raise ValueError(f"{path} must be <= {schema['maximum']}")
        elif expected == "string":
            if not isinstance(value, str):
                raise ValueError(f"{path} must be a string")
            if "minLength" in schema and len(value) < schema["minLength"]:
                raise ValueError(f"{path} is too short")
            if "maxLength" in schema and len(value) > schema["maxLength"]:
                raise ValueError(f"{path} is too long")
        elif expected == "boolean":
            if not isinstance(value, bool):
                raise ValueError(f"{path} must be a boolean")
        elif expected == "array":
            if not isinstance(value, list):
                raise ValueError(f"{path} must be an array")
            item_schema = schema.get("items") or {}
            for idx, item in enumerate(value):
                ToolRegistry._validate_value(item, item_schema, f"{path}[{idx}]")
        elif expected == "object":
            if not isinstance(value, dict):
                raise ValueError(f"{path} must be an object")
        if "enum" in schema and value not in schema["enum"]:
            raise ValueError(f"{path} has an unsupported value")

    @classmethod
    def _validate_args(cls, args: dict[str, Any], schema: dict[str, Any]) -> None:
        if not isinstance(args, dict):
            raise ValueError("tool args must be an object")
        props = schema.get("properties") or {}
        for required in schema.get("required") or []:
            if required not in args:
                raise ValueError(f"missing required tool argument: {required}")
        for key, value in args.items():
            if key in props:
                cls._validate_value(value, props[key], key)
            elif schema.get("additionalProperties") is False:
                raise ValueError(f"unknown tool argument: {key}")

    async def call(self, name: str, args: dict[str, Any], allowed_permissions: set[str]) -> Any:
        tool = self.tools.get(name)
        if not tool:
            raise ValueError(f"unknown tool: {name}")
        if tool.permission not in allowed_permissions:
            raise PermissionError(f"tool permission denied: {name}")
        self._validate_args(args, tool.parameters)
        result = tool.handler(**args)
        if inspect.isawaitable(result):
            result = await result
        return result


class AgentRunner:
    """Bounded, auditable tool loop. Provider adapters normalize tool messages."""

    MAX_TOOL_RESULT_CHARS = 60_000
    MAX_TOOL_CONTEXT_CHARS = 240_000
    MAX_TOOL_CALLS_PER_STEP = 8
    MAX_TOOL_CALLS_TOTAL = 24

    def __init__(self, llm, registry: ToolRegistry):
        self.llm = llm
        self.registry = registry

    @classmethod
    def _clip_tool_value(cls, value: Any, budget: int | None = None) -> Any:
        """Bound tool payloads so a curious model cannot overflow context by repeatedly reading full chapters."""
        budget = budget or cls.MAX_TOOL_RESULT_CHARS
        if isinstance(value, str):
            return value if len(value) <= min(12_000, budget) else value[:min(12_000, budget)] + "\n…[已截断，请缩小查询范围]"
        if isinstance(value, list):
            out = []
            used = 2
            for item in value[:80]:
                clipped = cls._clip_tool_value(item, max(2_000, min(12_000, budget - used)))
                size = len(json.dumps(clipped, ensure_ascii=False, default=str))
                if out and used + size > budget:
                    break
                out.append(clipped)
                used += size
                if used >= budget:
                    break
            if len(out) < len(value):
                out.append({"_truncated": True, "remaining_items": max(0, len(value) - len(out))})
            return out
        if isinstance(value, dict):
            out: dict[str, Any] = {}
            used = 2
            for key, item in value.items():
                clipped = cls._clip_tool_value(item, max(1_000, budget - used))
                size = len(str(key)) + len(json.dumps(clipped, ensure_ascii=False, default=str))
                if out and used + size > budget:
                    out["_truncated"] = True
                    break
                out[str(key)] = clipped
                used += size
            return out
        return value

    async def run(
        self,
        *,
        system: str,
        user: str | list[dict[str, Any]],
        permissions: set[str] | None = None,
        max_steps: int | None = None,
        event_hook: EventHook | None = None,
        effort: str = "high",
        max_tokens: int = 6000,
    ) -> str:
        permissions = permissions or {"read"}
        max_steps = min(max_steps or settings.max_agent_steps, settings.max_agent_steps)
        messages: list[dict[str, Any]] = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        schemas = self.registry.schemas(permissions)
        tool_context_chars = 0
        tool_calls_total = 0
        tool_cache: dict[str, Any] = {}
        for step in range(1, max_steps + 1):
            response = await self.llm.complete(
                messages,
                thinking=True,
                reasoning_effort=effort,
                max_tokens=max_tokens,
                tools=schemas,
                tool_choice="auto",
            )
            assistant_message = response.raw_message or {"role": "assistant", "content": response.content, "tool_calls": response.tool_calls}
            messages.append(assistant_message)
            if not response.tool_calls:
                if event_hook:
                    await event_hook(step, "reply", "final", {"chars": len(response.content)})
                return response.content
            for call_index, tool_call in enumerate(response.tool_calls):
                fn = tool_call.get("function", {})
                name = fn.get("name", "")
                raw_args = fn.get("arguments") or "{}"
                over_call_budget = call_index >= self.MAX_TOOL_CALLS_PER_STEP or tool_calls_total >= self.MAX_TOOL_CALLS_TOTAL
                if not over_call_budget:
                    tool_calls_total += 1
                try:
                    args = json.loads(raw_args, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"invalid JSON constant: {value}")))
                    if not isinstance(args, dict):
                        raise ValueError("tool args must be an object")
                    cache_key = json.dumps([name, args], ensure_ascii=False, sort_keys=True, default=str)
                    if over_call_budget:
                        payload = {"ok": False, "error": "本轮资料核对已经达到安全上限。请基于已取得的证据继续判断，不要继续扩大检索范围。"}
                    elif tool_context_chars >= self.MAX_TOOL_CONTEXT_CHARS:
                        payload = {"ok": False, "error": "本轮已读取足够多的作品资料，请基于现有证据回答；如仍缺信息，请缩小查询范围。"}
                    elif cache_key in tool_cache:
                        payload = {"ok": True, "result": tool_cache[cache_key], "cached": True}
                    else:
                        result = await self.registry.call(name, args, permissions)
                        clipped = self._clip_tool_value(result)
                        tool_cache[cache_key] = clipped
                        payload = {"ok": True, "result": clipped}
                    # Count every payload that becomes part of the model-visible tool context,
                    # including cached reads, because context size—not database work—is the hard limit.
                    tool_context_chars += len(json.dumps(payload, ensure_ascii=False, default=str))
                except Exception as exc:
                    payload = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
                if event_hook:
                    await event_hook(step, name, "tool_call", payload)
                messages.append({"role": "tool", "tool_call_id": tool_call.get("id", ""), "name": name, "content": json.dumps(payload, ensure_ascii=False, default=str)})
        raise RuntimeError(f"task exceeded max_steps={max_steps}")

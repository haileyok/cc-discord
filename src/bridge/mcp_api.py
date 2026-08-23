"""Security boundary for Polytoken MCP access to the live Slack bridge."""
from __future__ import annotations

import hashlib
import json
import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Awaitable, Callable, Mapping

from bridge.redaction import safe_error
from bridge.tasks import TaskNotFound, TaskPrivilegeError, TaskRestartError, TaskRoutingError, TaskSpawnError

log = logging.getLogger(__name__)


class McpCapability(StrEnum):
    READ = "read"
    WRITE = "write"
    CONTROL = "control"
    DESTRUCTIVE = "destructive"
    CROSS_CHANNEL = "cross_channel"
    CHANNEL_ADMIN = "channel_admin"


@dataclass(frozen=True, slots=True)
class McpContext:
    owner_user_id: str
    team_id: str
    request_id: str
    capabilities: frozenset[McpCapability] = frozenset({McpCapability.READ})
    source: str = "polytoken-mcp"


class McpApiError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable

    def as_dict(self) -> dict[str, Any]:
        return {"ok": False, "error": {"code": self.code, "message": str(self), "retryable": self.retryable}}


@dataclass(slots=True)
class _CacheEntry:
    expires_at: float
    fingerprint: str
    result: dict[str, Any]


class SlidingWindowLimiter:
    def __init__(self, *, limit: int = 60, window_secs: float = 60.0) -> None:
        if limit <= 0 or window_secs <= 0:
            raise ValueError("rate limit and window must be positive")
        self.limit = limit
        self.window_secs = window_secs
        self._calls: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str, *, now: float | None = None) -> None:
        current = time.monotonic() if now is None else now
        calls = self._calls[key]
        cutoff = current - self.window_secs
        while calls and calls[0] <= cutoff:
            calls.popleft()
        if len(calls) >= self.limit:
            raise McpApiError("rate_limited", "MCP request rate limit exceeded", retryable=True)
        calls.append(current)


@dataclass(slots=True)
class McpFacade:
    bot: Any
    registry: Any
    owner_user_id: str
    team_id: str
    limiter: SlidingWindowLimiter = field(default_factory=SlidingWindowLimiter)
    idempotency_ttl_secs: float = 600.0
    _idempotency: dict[str, _CacheEntry] = field(default_factory=dict, init=False, repr=False)
    _TOOLS: dict[str, tuple[McpCapability, str]] = field(default_factory=lambda: {
        "bridge_health": (McpCapability.READ, "_bridge_health"),
        "bridge_list_tasks": (McpCapability.READ, "_list_tasks"),
        "bridge_task_status": (McpCapability.READ, "_task_status"),
        "bridge_compact_task": (McpCapability.CONTROL, "_compact_task"),
        "bridge_set_model": (McpCapability.CONTROL, "_set_model"),
        "bridge_set_facet": (McpCapability.CONTROL, "_set_facet"),
        "bridge_set_effort": (McpCapability.CONTROL, "_set_effort"),
        "bridge_stop_task": (McpCapability.DESTRUCTIVE, "_stop_task"),
        "bridge_clear_context": (McpCapability.DESTRUCTIVE, "_clear_context"),
        "slack_read_thread": (McpCapability.READ, "_read_thread"),
        "slack_post_message": (McpCapability.WRITE, "_post_message"),
        "slack_edit_message": (McpCapability.WRITE, "_edit_message"),
        "slack_add_reaction": (McpCapability.WRITE, "_add_reaction"),
        "slack_remove_reaction": (McpCapability.WRITE, "_remove_reaction"),
        "slack_delete_message": (McpCapability.DESTRUCTIVE, "_delete_message"),
    }, init=False, repr=False)

    async def call(self, tool: str, arguments: Mapping[str, Any], ctx: McpContext) -> dict[str, Any]:
        started = time.monotonic()
        if ctx.owner_user_id != self.owner_user_id or ctx.team_id != self.team_id:
            raise McpApiError("forbidden", "MCP principal is not authorized")
        spec = self._TOOLS.get(str(tool))
        if spec is None:
            raise McpApiError("unknown_tool", "Unknown bridge MCP tool")
        capability, method_name = spec
        if capability not in ctx.capabilities:
            raise McpApiError("capability_denied", f"Tool requires {capability.value} capability")
        self.limiter.check(f"{ctx.owner_user_id}:{tool}")
        args = dict(arguments)
        fingerprint = self._fingerprint(tool, args)
        cached = self._cached(ctx.request_id, fingerprint)
        if cached is not None:
            return cached
        try:
            method: Callable[..., Awaitable[dict[str, Any]]] = getattr(self, method_name)
            result = await method(ctx, args)
        except McpApiError:
            raise
        except TaskNotFound as exc:
            raise McpApiError("task_not_found", "Task not found") from exc
        except TaskPrivilegeError as exc:
            raise McpApiError("forbidden", "Task is not owned by the MCP principal") from exc
        except TaskRestartError as exc:
            raise McpApiError("unsupported", "Requested task operation is unsupported") from exc
        except (TaskSpawnError, TaskRoutingError) as exc:
            raise McpApiError("task_operation_failed", safe_error(exc, "Task operation failed")) from exc
        except Exception as exc:
            log.warning("MCP facade call failed tool=%s request=%s: %s", tool, ctx.request_id[:12], safe_error(exc, "operation failed"))
            raise McpApiError("internal_error", "Bridge MCP operation failed", retryable=True) from exc
        response = {"ok": True, "result": result}
        self._remember(ctx.request_id, fingerprint, response)
        log.info("MCP audit tool=%s request=%s outcome=ok duration_ms=%d", tool, ctx.request_id[:12], int((time.monotonic() - started) * 1000))
        return response

    @staticmethod
    def _fingerprint(tool: str, arguments: Mapping[str, Any]) -> str:
        encoded = json.dumps({"tool": tool, "arguments": arguments}, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(encoded.encode()).hexdigest()

    def _cached(self, request_id: str, fingerprint: str) -> dict[str, Any] | None:
        now = time.monotonic()
        for key in [key for key, value in self._idempotency.items() if value.expires_at <= now]:
            self._idempotency.pop(key, None)
        entry = self._idempotency.get(request_id)
        if entry is None:
            return None
        if entry.fingerprint != fingerprint:
            raise McpApiError("idempotency_conflict", "Request ID was reused with different arguments")
        return entry.result

    def _remember(self, request_id: str, fingerprint: str, result: dict[str, Any]) -> None:
        if request_id:
            self._idempotency[request_id] = _CacheEntry(time.monotonic() + self.idempotency_ttl_secs, fingerprint, result)

    @staticmethod
    def _text(args: Mapping[str, Any], key: str, *, limit: int = 200, required: bool = True) -> str:
        value = str(args.get(key) or "").strip()
        if required and not value:
            raise McpApiError("invalid_arguments", f"{key} is required")
        if len(value) > limit:
            raise McpApiError("invalid_arguments", f"{key} exceeds {limit} characters")
        return value

    def _owned_task(self, ctx: McpContext, task_id: str) -> Any:
        task = self.registry.get_by_task_id(task_id)
        if task is None:
            raise TaskNotFound(task_id)
        if task.owner_user_id != ctx.owner_user_id or task.team_id != ctx.team_id:
            raise TaskPrivilegeError(task_id)
        return task

    @staticmethod
    def _task_summary(task: Any) -> dict[str, Any]:
        return {"task_id": str(task.task_id), "session_id": str(task.polytoken_session_id) if task.polytoken_session_id else None, "channel_id": str(task.channel_id), "root_ts": str(task.root_ts), "status": str(task.status), "mode": str(task.mode), "last_activity": int(task.last_activity or 0), "turn_active": bool(task.progress_started), "compaction_pending": bool(task.compaction_pending)}

    @staticmethod
    def _safe_state(state: Mapping[str, Any] | None) -> dict[str, Any]:
        source = dict(state or {})
        allowed = {"session_title", "active_model", "active_reasoning_effort", "active_facet", "turn_in_flight", "context_usage", "todos", "pending_interrogatives", "goal", "compaction"}
        result = {key: source[key] for key in allowed if key in source}
        if len(json.dumps(result, ensure_ascii=False, default=str)) > 24_000:
            return {"truncated": True, "turn_in_flight": bool(source.get("turn_in_flight")), "session_title": str(source.get("session_title") or "")[:200]}
        return result

    async def _bridge_health(self, ctx: McpContext, args: Mapping[str, Any]) -> dict[str, Any]:
        health = self.bot.health() if callable(getattr(self.bot, "health", None)) else {}
        safe = {key: value for key, value in dict(health or {}).items() if "token" not in key.lower() and "secret" not in key.lower()}
        tasks = await self.registry.list_tasks(owner_user_id=ctx.owner_user_id)
        safe["active_task_count"] = len(tasks)
        return safe

    async def _list_tasks(self, ctx: McpContext, args: Mapping[str, Any]) -> dict[str, Any]:
        tasks = await self.registry.list_tasks(owner_user_id=ctx.owner_user_id)
        return {"tasks": [self._task_summary(task) for task in tasks[:100]], "count": min(len(tasks), 100)}

    async def _task_status(self, ctx: McpContext, args: Mapping[str, Any]) -> dict[str, Any]:
        task_id = self._text(args, "task_id", limit=80)
        task = self._owned_task(ctx, task_id)
        state = await self.registry.get_state(task_id, ctx.owner_user_id)
        return {"task": self._task_summary(task), "state": self._safe_state(state)}

    async def _compact_task(self, ctx: McpContext, args: Mapping[str, Any]) -> dict[str, Any]:
        task_id = self._text(args, "task_id", limit=80)
        self._owned_task(ctx, task_id)
        outcome = await self.registry.request_compaction(task_id, owner_user_id=ctx.owner_user_id)
        return {"task_id": task_id, "outcome": outcome}

    async def _set_model(self, ctx: McpContext, args: Mapping[str, Any]) -> dict[str, Any]:
        task_id = self._text(args, "task_id", limit=80); model = self._text(args, "model", limit=200)
        effort = self._text(args, "reasoning_effort", limit=32, required=False) or None
        self._owned_task(ctx, task_id)
        await self.registry.set_model(task_id, model, owner_user_id=ctx.owner_user_id, reasoning_effort=effort)
        return {"task_id": task_id, "model": model, "reasoning_effort": effort}

    async def _set_facet(self, ctx: McpContext, args: Mapping[str, Any]) -> dict[str, Any]:
        task_id = self._text(args, "task_id", limit=80); facet = self._text(args, "facet", limit=80)
        self._owned_task(ctx, task_id); await self.registry.set_facet(task_id, facet, owner_user_id=ctx.owner_user_id)
        return {"task_id": task_id, "facet": facet}

    async def _set_effort(self, ctx: McpContext, args: Mapping[str, Any]) -> dict[str, Any]:
        task_id = self._text(args, "task_id", limit=80); effort = self._text(args, "effort", limit=32)
        self._owned_task(ctx, task_id); await self.registry.set_effort(task_id, effort, owner_user_id=ctx.owner_user_id)
        return {"task_id": task_id, "effort": effort}

    async def _stop_task(self, ctx: McpContext, args: Mapping[str, Any]) -> dict[str, Any]:
        task_id = self._text(args, "task_id", limit=80)
        if args.get("confirm") is not True:
            raise McpApiError("confirmation_required", "stop_task requires confirm=true")
        self._owned_task(ctx, task_id)
        return {"task_id": task_id, "stopped": bool(await self.registry.stop_task(task_id, ctx.owner_user_id))}

    async def _clear_context(self, ctx: McpContext, args: Mapping[str, Any]) -> dict[str, Any]:
        task_id = self._text(args, "task_id", limit=80)
        if args.get("confirm") is not True:
            raise McpApiError("confirmation_required", "clear_context requires confirm=true")
        self._owned_task(ctx, task_id); await self.registry.clear_context(task_id, owner_user_id=ctx.owner_user_id)
        return {"task_id": task_id, "cleared": True}

    @staticmethod
    def _next_cursor(page: Mapping[str, Any]) -> str | None:
        metadata = page.get("response_metadata")
        if isinstance(metadata, Mapping):
            value = str(metadata.get("next_cursor") or "").strip()
            return value or None
        return None

    @staticmethod
    def _safe_message(message: Mapping[str, Any]) -> dict[str, Any]:
        files = []
        for item in message.get("files") or []:
            if isinstance(item, Mapping):
                files.append({key: item.get(key) for key in ("id", "name", "title", "mimetype", "size", "permalink") if item.get(key) is not None})
        return {
            "ts": str(message.get("ts") or ""),
            "thread_ts": str(message.get("thread_ts") or "") or None,
            "actor_id": str(message.get("user") or message.get("bot_id") or "") or None,
            "text": str(message.get("text") or "")[:8_000],
            "files": files[:20],
        }

    async def _thread_messages(self, task: Any, *, max_messages: int = 200) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(5):
            page = await self.bot.fetch_thread_replies(task.channel_id, task.root_ts, cursor=cursor, limit=min(100, max_messages - len(messages)))
            for item in page.get("messages") or []:
                if isinstance(item, Mapping):
                    messages.append(self._safe_message(item))
                    if len(messages) >= max_messages:
                        return messages
            cursor = self._next_cursor(page)
            if not cursor:
                break
        return messages

    async def _require_thread_message(self, task: Any, message_ts: str) -> None:
        if message_ts == task.root_ts:
            return
        messages = await self._thread_messages(task)
        if not any(item["ts"] == message_ts for item in messages):
            raise McpApiError("message_not_in_task", "Message is not in the owned task thread")

    async def _read_thread(self, ctx: McpContext, args: Mapping[str, Any]) -> dict[str, Any]:
        task_id = self._text(args, "task_id", limit=80)
        task = self._owned_task(ctx, task_id)
        try:
            maximum = max(1, min(int(args.get("limit", 100)), 200))
        except (TypeError, ValueError) as exc:
            raise McpApiError("invalid_arguments", "limit must be an integer") from exc
        messages = await self._thread_messages(task, max_messages=maximum)
        return {"task_id": task_id, "channel_id": task.channel_id, "root_ts": task.root_ts, "messages": messages, "count": len(messages)}

    async def _post_message(self, ctx: McpContext, args: Mapping[str, Any]) -> dict[str, Any]:
        task_id = self._text(args, "task_id", limit=80); text = self._text(args, "text", limit=32_000)
        task = self._owned_task(ctx, task_id)
        ids = await self.bot.post(text, channel_id=task.channel_id, root_ts=task.root_ts)
        return {"task_id": task_id, "message_ts": ids[0] if ids else None, "message_count": len(ids)}

    async def _edit_message(self, ctx: McpContext, args: Mapping[str, Any]) -> dict[str, Any]:
        task_id = self._text(args, "task_id", limit=80); message_ts = self._text(args, "message_ts", limit=40)
        text = self._text(args, "text", limit=32_000); task = self._owned_task(ctx, task_id)
        await self._require_thread_message(task, message_ts)
        await self.bot.edit_message(task.channel_id, message_ts, text=text)
        return {"task_id": task_id, "message_ts": message_ts, "edited": True}

    async def _add_reaction(self, ctx: McpContext, args: Mapping[str, Any]) -> dict[str, Any]:
        return await self._reaction(ctx, args, remove=False)

    async def _remove_reaction(self, ctx: McpContext, args: Mapping[str, Any]) -> dict[str, Any]:
        return await self._reaction(ctx, args, remove=True)

    async def _reaction(self, ctx: McpContext, args: Mapping[str, Any], *, remove: bool) -> dict[str, Any]:
        task_id = self._text(args, "task_id", limit=80); message_ts = self._text(args, "message_ts", limit=40)
        emoji = self._text(args, "emoji", limit=80); task = self._owned_task(ctx, task_id)
        await self._require_thread_message(task, message_ts)
        method = self.bot.remove_reaction if remove else self.bot.add_reaction
        await method(task.channel_id, message_ts, emoji)
        return {"task_id": task_id, "message_ts": message_ts, "emoji": emoji.strip(":"), "removed": remove}

    async def _delete_message(self, ctx: McpContext, args: Mapping[str, Any]) -> dict[str, Any]:
        task_id = self._text(args, "task_id", limit=80); message_ts = self._text(args, "message_ts", limit=40)
        if args.get("confirm") is not True:
            raise McpApiError("confirmation_required", "delete_message requires confirm=true")
        task = self._owned_task(ctx, task_id); await self._require_thread_message(task, message_ts)
        await self.bot.delete_message(task.channel_id, message_ts)
        return {"task_id": task_id, "message_ts": message_ts, "deleted": True}


__all__ = ["McpApiError", "McpCapability", "McpContext", "McpFacade", "SlidingWindowLimiter"]

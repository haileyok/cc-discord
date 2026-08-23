"""Security boundary for Polytoken MCP access to the live Slack bridge."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

from bridge.bot import slack_error_code
from bridge.redaction import safe_error
from bridge.tasks import (
    ATTACHMENTS_DIR,
    MAX_ATTACHMENT_BYTES,
    TaskNotFound,
    TaskPrivilegeError,
    TaskRestartError,
    TaskRoutingError,
    TaskSpawnError,
)

log = logging.getLogger(__name__)
MCP_CANVAS_MARKDOWN_LIMIT = 60_000


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
    _inflight: dict[str, tuple[str, asyncio.Task[dict[str, Any]]]] = field(default_factory=dict, init=False, repr=False)
    _inflight_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _canvas_tasks: dict[str, str] = field(default_factory=dict, init=False, repr=False)
    _scheduled_tasks: dict[str, str] = field(default_factory=dict, init=False, repr=False)
    _polls: dict[str, dict[str, Any]] = field(default_factory=dict, init=False, repr=False)
    _TOOLS: dict[str, tuple[McpCapability, str]] = field(default_factory=lambda: {
        "bridge_health": (McpCapability.READ, "_bridge_health"),
        "bridge_list_tasks": (McpCapability.READ, "_list_tasks"),
        "bridge_task_status": (McpCapability.READ, "_task_status"),
        "bridge_compact_task": (McpCapability.CONTROL, "_compact_task"),
        "bridge_cancel_turn": (McpCapability.CONTROL, "_cancel_turn"),
        "bridge_promote_task": (McpCapability.CHANNEL_ADMIN, "_promote_task"),
        "bridge_set_model": (McpCapability.CONTROL, "_set_model"),
        "bridge_set_facet": (McpCapability.CONTROL, "_set_facet"),
        "bridge_set_effort": (McpCapability.CONTROL, "_set_effort"),
        "bridge_stop_task": (McpCapability.DESTRUCTIVE, "_stop_task"),
        "bridge_clear_context": (McpCapability.DESTRUCTIVE, "_clear_context"),
        "slack_read_thread": (McpCapability.READ, "_read_thread"),
        "slack_read_channel_history": (McpCapability.CROSS_CHANNEL, "_read_channel_history"),
        "slack_search_task_messages": (McpCapability.READ, "_search_task_messages"),
        "slack_post_message": (McpCapability.WRITE, "_post_message"),
        "slack_upload_file": (McpCapability.WRITE, "_upload_file"),
        "slack_download_thread_file": (McpCapability.READ, "_download_thread_file"),
        "slack_create_canvas": (McpCapability.WRITE, "_create_canvas"),
        "slack_edit_canvas": (McpCapability.WRITE, "_edit_canvas"),
        "slack_set_channel_metadata": (McpCapability.CHANNEL_ADMIN, "_set_channel_metadata"),
        "slack_invite_participants": (McpCapability.CHANNEL_ADMIN, "_invite_participants"),
        "slack_remove_participants": (McpCapability.DESTRUCTIVE, "_remove_participants"),
        "slack_add_bookmark": (McpCapability.CHANNEL_ADMIN, "_add_bookmark"),
        "slack_remove_bookmark": (McpCapability.DESTRUCTIVE, "_remove_bookmark"),
        "slack_schedule_message": (McpCapability.WRITE, "_schedule_message"),
        "slack_list_scheduled_messages": (McpCapability.READ, "_list_scheduled_messages"),
        "slack_cancel_scheduled_message": (McpCapability.DESTRUCTIVE, "_cancel_scheduled_message"),
        "slack_create_poll": (McpCapability.WRITE, "_create_poll"),
        "slack_create_approval": (McpCapability.WRITE, "_create_approval"),
        "slack_get_poll_results": (McpCapability.READ, "_get_poll_results"),
        "slack_edit_message": (McpCapability.WRITE, "_edit_message"),
        "slack_add_reaction": (McpCapability.WRITE, "_add_reaction"),
        "slack_remove_reaction": (McpCapability.WRITE, "_remove_reaction"),
        "slack_delete_message": (McpCapability.DESTRUCTIVE, "_delete_message"),
    }, init=False, repr=False)

    async def call(self, tool: str, arguments: Mapping[str, Any], ctx: McpContext) -> dict[str, Any]:
        started = time.monotonic()
        outcome = "ok"
        try:
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
            if not ctx.request_id:
                return await self._execute_call(tool, method_name, args, ctx, fingerprint)
            async with self._inflight_lock:
                cached = self._cached(ctx.request_id, fingerprint)
                if cached is not None:
                    outcome = "cached"
                    return cached
                pending = self._inflight.get(ctx.request_id)
                if pending is not None:
                    pending_fingerprint, task = pending
                    if pending_fingerprint != fingerprint:
                        raise McpApiError(
                            "idempotency_conflict",
                            "Request ID was reused with different arguments",
                        )
                    outcome = "deduplicated"
                else:
                    task = asyncio.create_task(
                        self._execute_call(tool, method_name, args, ctx, fingerprint),
                        name=f"mcp-{tool}-{ctx.request_id[:12]}",
                    )
                    self._inflight[ctx.request_id] = (fingerprint, task)
                    task.add_done_callback(
                        lambda done, request_id=ctx.request_id: self._clear_inflight(
                            request_id, done
                        )
                    )
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except McpApiError as exc:
            outcome = exc.code
            raise
        finally:
            log.info(
                "MCP audit tool=%s request=%s outcome=%s duration_ms=%d",
                str(tool)[:100], ctx.request_id[:12], outcome,
                int((time.monotonic() - started) * 1000),
            )

    def _clear_inflight(self, request_id: str, task: asyncio.Task[dict[str, Any]]) -> None:
        # asyncio callbacks run synchronously on this event loop. The guarded
        # request path has no await while it reads/writes this dict, so cleanup
        # cannot interleave with a partial critical-section update.
        current = self._inflight.get(request_id)
        if current is not None and current[1] is task:
            self._inflight.pop(request_id, None)

    async def _execute_call(self, tool: str, method_name: str, args: Mapping[str, Any],
                            ctx: McpContext, fingerprint: str) -> dict[str, Any]:
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
        active_work = []
        for handle, block in list(getattr(task, "subagent_blocks", {}).items())[:20]:
            active_work.append({
                "handle": str(handle)[:100],
                "type": str(getattr(block, "attribution", "subagent"))[:100],
                "status": "complete" if getattr(block, "finished_at", None) else "running",
                "recent_activity": [str(line)[:300] for line in list(getattr(block, "actions", []))[-5:]],
            })
        return {
            "task_id": str(task.task_id),
            "session_id": str(task.polytoken_session_id) if task.polytoken_session_id else None,
            "channel_id": str(task.channel_id), "root_ts": str(task.root_ts),
            "status": str(task.status), "mode": str(task.mode),
            "last_activity": int(task.last_activity or 0),
            "turn_active": bool(task.progress_started),
            "compaction_pending": bool(task.compaction_pending),
            "active_work": active_work,
        }

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
        tasks = [task for task in tasks if task.team_id == ctx.team_id]
        safe["active_task_count"] = len(tasks)
        return safe

    async def _list_tasks(self, ctx: McpContext, args: Mapping[str, Any]) -> dict[str, Any]:
        tasks = await self.registry.list_tasks(owner_user_id=ctx.owner_user_id)
        tasks = [task for task in tasks if task.team_id == ctx.team_id]
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

    async def _cancel_turn(self, ctx: McpContext, args: Mapping[str, Any]) -> dict[str, Any]:
        task_id = self._text(args, "task_id", limit=80)
        if args.get("confirm") is not True:
            raise McpApiError("confirmation_required", "cancel_turn requires confirm=true")
        self._owned_task(ctx, task_id)
        cancelled = await self.registry.cancel_turn(task_id, ctx.owner_user_id)
        return {"task_id": task_id, "cancelled": bool(cancelled), "session_preserved": True}

    async def _promote_task(self, ctx: McpContext, args: Mapping[str, Any]) -> dict[str, Any]:
        task_id = self._text(args, "task_id", limit=80)
        task = self._owned_task(ctx, task_id)
        raw_users = args.get("participant_user_ids")
        users: list[str] | None = None
        if raw_users is not None:
            if not isinstance(raw_users, list) or len(raw_users) > 50:
                raise McpApiError(
                    "invalid_arguments",
                    "participant_user_ids must be a list of at most 50 Slack IDs",
                )
            users = list(dict.fromkeys(
                str(item).strip() for item in raw_users if str(item).strip()
            ))
            if any(len(item) > 100 for item in users):
                raise McpApiError("invalid_arguments", "participant_user_ids contains an invalid ID")
        name = self._text(args, "name", limit=80, required=False) or None
        plan = {
            "task_id": task_id,
            "current_mode": str(task.mode),
            "action": "create_private_channel_and_rebind_task",
            "channel_name": name or f"task-{task_id[:8]}",
            "participant_user_ids": users,
            "guards": ["pending_questions_must_be_resolved", "journaled_rollback"],
        }
        if args.get("dry_run", True) is not False:
            return {"dry_run": True, "plan": plan}
        if args.get("confirm") is not True:
            raise McpApiError("confirmation_required", "promote_task execution requires confirm=true")
        promoted = await self.registry.promote_task(
            task_id, ctx.owner_user_id, users, name=name
        )
        return {"dry_run": False, "promoted": True,
                "task": self._task_summary(promoted)}

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
        cursor: str | None = None
        for _ in range(5):
            page = await self.bot.fetch_thread_replies(
                task.channel_id, task.root_ts, cursor=cursor, limit=100
            )
            if any(
                isinstance(item, Mapping) and str(item.get("ts") or "") == message_ts
                for item in page.get("messages") or []
            ):
                return
            cursor = self._next_cursor(page)
            if not cursor:
                break
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

    async def _read_channel_history(self, ctx: McpContext, args: Mapping[str, Any]) -> dict[str, Any]:
        task_id = self._text(args, "task_id", limit=80)
        task = self._owned_task(ctx, task_id)
        try:
            maximum = max(1, min(int(args.get("limit", 100)), 200))
        except (TypeError, ValueError) as exc:
            raise McpApiError("invalid_arguments", "limit must be an integer") from exc
        messages: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(2):
            page = await self.bot.fetch_channel_history(
                task.channel_id, cursor=cursor, limit=min(100, maximum - len(messages))
            )
            messages.extend(
                self._safe_message(item) for item in page.get("messages") or []
                if isinstance(item, Mapping)
            )
            if len(messages) >= maximum:
                break
            cursor = self._next_cursor(page)
            if not cursor:
                break
        return {"task_id": task_id, "channel_id": task.channel_id,
                "messages": messages[:maximum], "count": min(len(messages), maximum)}

    async def _search_task_messages(self, ctx: McpContext, args: Mapping[str, Any]) -> dict[str, Any]:
        task_id = self._text(args, "task_id", limit=80)
        query = self._text(args, "query", limit=500).casefold()
        task = self._owned_task(ctx, task_id)
        messages = await self._thread_messages(task, max_messages=200)
        matches = [item for item in messages if query in str(item.get("text") or "").casefold()]
        try:
            maximum = max(1, min(int(args.get("limit", 25)), 100))
        except (TypeError, ValueError) as exc:
            raise McpApiError("invalid_arguments", "limit must be an integer") from exc
        return {"task_id": task_id, "query": query, "matches": matches[:maximum],
                "count": min(len(matches), maximum), "truncated": len(matches) > maximum}

    async def _post_message(self, ctx: McpContext, args: Mapping[str, Any]) -> dict[str, Any]:
        task_id = self._text(args, "task_id", limit=80); text = self._text(args, "text", limit=32_000)
        task = self._owned_task(ctx, task_id)
        ids = await self.bot.post(text, channel_id=task.channel_id, root_ts=task.root_ts)
        return {"task_id": task_id, "message_ts": ids[0] if ids else None, "message_count": len(ids)}

    async def _upload_file(self, ctx: McpContext, args: Mapping[str, Any]) -> dict[str, Any]:
        task_id = self._text(args, "task_id", limit=80)
        path = Path(self._text(args, "path", limit=4096)).expanduser()
        task = self._owned_task(ctx, task_id)
        title = str(args.get("title") or "").strip()[:500] or None
        comment = str(args.get("initial_comment") or "").strip()[:8_000] or None
        result = await self.bot.upload_file(path, channel_id=task.channel_id, root_ts=task.root_ts,
                                            title=title, initial_comment=comment)
        file_id = None
        if isinstance(result, Mapping):
            file_id = result.get("file_id") or (result.get("file") or {}).get("id")
        return {"task_id": task_id, "file_id": str(file_id) if file_id else None,
                "name": path.name, "uploaded": True}

    async def _download_thread_file(self, ctx: McpContext, args: Mapping[str, Any]) -> dict[str, Any]:
        task_id = self._text(args, "task_id", limit=80)
        file_id = self._text(args, "file_id", limit=100)
        task = self._owned_task(ctx, task_id)
        cursor: str | None = None
        selected: Mapping[str, Any] | None = None
        for _ in range(5):
            page = await self.bot.fetch_thread_replies(task.channel_id, task.root_ts,
                                                       cursor=cursor, limit=100)
            for message in page.get("messages") or []:
                if not isinstance(message, Mapping):
                    continue
                for item in message.get("files") or []:
                    if isinstance(item, Mapping) and str(item.get("id") or "") == file_id:
                        selected = item
                        break
                if selected is not None:
                    break
            if selected is not None:
                break
            cursor = self._next_cursor(page)
            if not cursor:
                break
        if selected is None:
            raise McpApiError("file_not_in_task", "File is not in the owned task thread")
        url = str(selected.get("url_private_download") or selected.get("url_private") or "")
        if not url:
            raise McpApiError("file_unavailable", "Slack file has no authenticated download URL")
        filename = Path(str(selected.get("name") or selected.get("title") or file_id)).name
        directory = ATTACHMENTS_DIR / task_id / "mcp-downloads"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination = directory / f"{file_id}-{filename}"
        await self.bot.download_file(url, destination, max_bytes=MAX_ATTACHMENT_BYTES)
        return {"task_id": task_id, "file_id": file_id, "name": filename,
                "size": destination.stat().st_size, "path": str(destination.resolve())}

    @staticmethod
    def _canvas_error(exc: Exception) -> McpApiError:
        if slack_error_code(exc) in {
            "missing_scope", "feature_disabled", "not_allowed_token_type",
            "method_not_supported_for_channel_type", "unknown_method",
        }:
            return McpApiError(
                "capability_unavailable",
                "Slack Canvas is unavailable for this workspace, channel, or token scope",
            )
        return McpApiError("canvas_operation_failed", "Slack Canvas operation failed", retryable=True)

    async def _create_canvas(self, ctx: McpContext, args: Mapping[str, Any]) -> dict[str, Any]:
        task_id = self._text(args, "task_id", limit=80)
        title = self._text(args, "title", limit=500)
        markdown = self._text(args, "markdown", limit=MCP_CANVAS_MARKDOWN_LIMIT)
        task = self._owned_task(ctx, task_id)
        try:
            result = await self.bot.create_canvas(
                title=title, markdown=markdown, channel_id=task.channel_id
            )
        except Exception as exc:
            raise self._canvas_error(exc) from exc
        canvas_id = str(result.get("canvas_id") or result.get("id") or "")
        if not canvas_id:
            raise McpApiError("canvas_operation_failed", "Slack did not return a Canvas ID")
        self._canvas_tasks[canvas_id] = task_id
        return {"task_id": task_id, "canvas_id": canvas_id, "title": title, "created": True}

    async def _edit_canvas(self, ctx: McpContext, args: Mapping[str, Any]) -> dict[str, Any]:
        task_id = self._text(args, "task_id", limit=80)
        canvas_id = self._text(args, "canvas_id", limit=100)
        operation = self._text(args, "operation", limit=40)
        self._owned_task(ctx, task_id)
        if self._canvas_tasks.get(canvas_id) != task_id:
            raise McpApiError(
                "canvas_not_in_task",
                "Canvas was not created for this owned task by the current bridge process",
            )
        if operation not in {"append", "prepend", "rename"}:
            raise McpApiError("invalid_arguments", "operation must be append, prepend, or rename")
        markdown = str(args.get("markdown") or "").strip()
        title = str(args.get("title") or "").strip()
        if operation == "rename":
            if not title:
                raise McpApiError("invalid_arguments", "title is required for rename")
            title = title[:500]
        elif not markdown:
            raise McpApiError("invalid_arguments", "markdown is required for append or prepend")
        elif len(markdown) > MCP_CANVAS_MARKDOWN_LIMIT:
            raise McpApiError(
                "invalid_arguments",
                f"markdown exceeds the MCP Canvas limit of {MCP_CANVAS_MARKDOWN_LIMIT} characters",
            )
        slack_operation = {"append": "insert_at_end", "prepend": "insert_at_start",
                           "rename": "rename"}[operation]
        try:
            await self.bot.edit_canvas(canvas_id, operation=slack_operation,
                                       markdown=markdown or None, title=title or None)
        except Exception as exc:
            raise self._canvas_error(exc) from exc
        return {"task_id": task_id, "canvas_id": canvas_id,
                "operation": operation, "edited": True}

    def _owned_managed_channel_task(self, ctx: McpContext, task_id: str) -> Any:
        task = self._owned_task(ctx, task_id)
        if not bool(getattr(task, "channel_owned", False)):
            raise McpApiError(
                "channel_not_managed",
                "Task is not bound to a bridge-owned private channel",
            )
        return task

    @staticmethod
    def _user_ids(args: Mapping[str, Any]) -> list[str]:
        raw = args.get("user_ids")
        if not isinstance(raw, list) or not raw or len(raw) > 50:
            raise McpApiError("invalid_arguments", "user_ids must be a non-empty list of at most 50 IDs")
        values = list(dict.fromkeys(str(item).strip() for item in raw if str(item).strip()))
        if not values or any(len(item) > 100 for item in values):
            raise McpApiError("invalid_arguments", "user_ids contains an invalid ID")
        return values

    async def _set_channel_metadata(self, ctx: McpContext, args: Mapping[str, Any]) -> dict[str, Any]:
        task_id = self._text(args, "task_id", limit=80)
        field_name = self._text(args, "field", limit=20)
        value = self._text(args, "value", limit=250)
        if field_name not in {"topic", "purpose"}:
            raise McpApiError("invalid_arguments", "field must be topic or purpose")
        task = self._owned_managed_channel_task(ctx, task_id)
        await self.bot.set_managed_channel_metadata(
            task.channel_id, **{field_name: value}
        )
        return {"task_id": task_id, "channel_id": task.channel_id,
                "field": field_name, "updated": True}

    async def _invite_participants(self, ctx: McpContext, args: Mapping[str, Any]) -> dict[str, Any]:
        task_id = self._text(args, "task_id", limit=80)
        users = self._user_ids(args)
        task = self._owned_managed_channel_task(ctx, task_id)
        await self.bot.invite_participants(task.channel_id, users)
        return {"task_id": task_id, "channel_id": task.channel_id,
                "user_ids": users, "invited": True}

    async def _remove_participants(self, ctx: McpContext, args: Mapping[str, Any]) -> dict[str, Any]:
        task_id = self._text(args, "task_id", limit=80)
        if args.get("confirm") is not True:
            raise McpApiError("confirmation_required", "remove_participants requires confirm=true")
        users = self._user_ids(args)
        if ctx.owner_user_id in users:
            raise McpApiError("invalid_arguments", "Task owner cannot be removed by MCP")
        task = self._owned_managed_channel_task(ctx, task_id)
        await self.bot.remove_participants(task.channel_id, users)
        return {"task_id": task_id, "channel_id": task.channel_id,
                "user_ids": users, "removed": True}

    async def _add_bookmark(self, ctx: McpContext, args: Mapping[str, Any]) -> dict[str, Any]:
        task_id = self._text(args, "task_id", limit=80)
        title = self._text(args, "title", limit=250)
        link = self._text(args, "link", limit=2_000)
        emoji = str(args.get("emoji") or "").strip()[:80] or None
        task = self._owned_managed_channel_task(ctx, task_id)
        result = await self.bot.add_managed_channel_bookmark(
            task.channel_id, title=title, link=link, emoji=emoji
        )
        bookmark = result.get("bookmark") if isinstance(result, Mapping) else None
        bookmark_id = bookmark.get("id") if isinstance(bookmark, Mapping) else None
        return {"task_id": task_id, "channel_id": task.channel_id,
                "bookmark_id": str(bookmark_id) if bookmark_id else None, "added": True}

    async def _remove_bookmark(self, ctx: McpContext, args: Mapping[str, Any]) -> dict[str, Any]:
        task_id = self._text(args, "task_id", limit=80)
        bookmark_id = self._text(args, "bookmark_id", limit=100)
        if args.get("confirm") is not True:
            raise McpApiError("confirmation_required", "remove_bookmark requires confirm=true")
        task = self._owned_managed_channel_task(ctx, task_id)
        await self.bot.remove_managed_channel_bookmark(task.channel_id, bookmark_id)
        return {"task_id": task_id, "channel_id": task.channel_id,
                "bookmark_id": bookmark_id, "removed": True}

    async def _schedule_message(self, ctx: McpContext, args: Mapping[str, Any]) -> dict[str, Any]:
        task_id = self._text(args, "task_id", limit=80)
        text = self._text(args, "text", limit=32_000)
        try:
            post_at = int(args.get("post_at"))
        except (TypeError, ValueError) as exc:
            raise McpApiError("invalid_arguments", "post_at must be a Unix timestamp") from exc
        now = int(time.time())
        if post_at <= now:
            raise McpApiError("invalid_arguments", "post_at must be in the future")
        if post_at > now + 120 * 86400:
            raise McpApiError("invalid_arguments", "post_at cannot exceed 120 days")
        task = self._owned_task(ctx, task_id)
        try:
            result = await self.bot.schedule_message(
                task.channel_id, task.root_ts, text=text, post_at=post_at
            )
        except Exception as exc:
            if slack_error_code(exc) == "restricted_too_many":
                raise McpApiError(
                    "rate_limited",
                    "Slack allows at most 30 scheduled messages per five-minute channel window",
                    retryable=True,
                ) from exc
            raise
        scheduled_id = str(result.get("scheduled_message_id") or "")
        if not scheduled_id:
            raise McpApiError("schedule_failed", "Slack did not return a scheduled message ID")
        self._scheduled_tasks[scheduled_id] = task_id
        return {"task_id": task_id, "scheduled_message_id": scheduled_id,
                "post_at": post_at, "scheduled": True}

    async def _list_scheduled_messages(self, ctx: McpContext, args: Mapping[str, Any]) -> dict[str, Any]:
        task_id = self._text(args, "task_id", limit=80)
        task = self._owned_task(ctx, task_id)
        messages: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(5):
            page = await self.bot.list_scheduled_messages(
                task.channel_id, cursor=cursor, limit=100
            )
            for item in page.get("scheduled_messages") or []:
                if not isinstance(item, Mapping):
                    continue
                scheduled_id = str(item.get("id") or item.get("scheduled_message_id") or "")
                if scheduled_id and self._scheduled_tasks.get(scheduled_id) == task_id:
                    messages.append({
                        "scheduled_message_id": scheduled_id,
                        "post_at": int(item.get("post_at") or 0),
                        "text": str(item.get("text") or "")[:8_000],
                    })
            cursor = self._next_cursor(page)
            if not cursor:
                break
        return {"task_id": task_id, "messages": messages[:100],
                "count": min(len(messages), 100),
                "restart_note": "Only schedules created by this bridge process are listed"}

    async def _cancel_scheduled_message(self, ctx: McpContext, args: Mapping[str, Any]) -> dict[str, Any]:
        task_id = self._text(args, "task_id", limit=80)
        scheduled_id = self._text(args, "scheduled_message_id", limit=200)
        if args.get("confirm") is not True:
            raise McpApiError("confirmation_required", "cancel_scheduled_message requires confirm=true")
        task = self._owned_task(ctx, task_id)
        if self._scheduled_tasks.get(scheduled_id) != task_id:
            raise McpApiError(
                "scheduled_message_not_in_task",
                "Scheduled message was not created for this task by the current bridge process",
            )
        found = False
        cursor: str | None = None
        for _ in range(5):
            page = await self.bot.list_scheduled_messages(task.channel_id, cursor=cursor, limit=100)
            found = any(
                str(item.get("id") or item.get("scheduled_message_id") or "") == scheduled_id
                for item in page.get("scheduled_messages") or [] if isinstance(item, Mapping)
            )
            if found:
                break
            cursor = self._next_cursor(page)
            if not cursor:
                break
        if not found:
            self._scheduled_tasks.pop(scheduled_id, None)
            raise McpApiError("scheduled_message_not_found", "Scheduled message is no longer pending")
        await self.bot.delete_scheduled_message(task.channel_id, scheduled_id)
        self._scheduled_tasks.pop(scheduled_id, None)
        return {"task_id": task_id, "scheduled_message_id": scheduled_id,
                "cancelled": True}

    @staticmethod
    def _poll_options(args: Mapping[str, Any]) -> tuple[list[str], list[str]]:
        raw_options = args.get("options")
        if not isinstance(raw_options, list) or not 2 <= len(raw_options) <= 10:
            raise McpApiError("invalid_arguments", "options must contain 2 to 10 labels")
        options = [str(item).strip() for item in raw_options]
        if any(not item or len(item) > 200 for item in options):
            raise McpApiError("invalid_arguments", "poll option labels must be 1 to 200 characters")
        raw_emojis = args.get("emojis")
        defaults = ["one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "keycap_ten"]
        emojis = defaults[:len(options)] if raw_emojis is None else [str(item).strip().strip(":") for item in raw_emojis]
        if len(emojis) != len(options) or len(set(emojis)) != len(emojis):
            raise McpApiError("invalid_arguments", "emojis must be unique and match options length")
        if any(not emoji or len(emoji) > 80 or not all(ch.isalnum() or ch in "_+-" for ch in emoji) for emoji in emojis):
            raise McpApiError("invalid_arguments", "poll contains an invalid emoji name")
        return options, emojis

    async def _create_poll_common(self, ctx: McpContext, args: Mapping[str, Any], *, kind: str) -> dict[str, Any]:
        task_id = self._text(args, "task_id", limit=80)
        question = self._text(args, "question", limit=1_000)
        options, emojis = self._poll_options(args)
        task = self._owned_task(ctx, task_id)
        lines = [f"*{kind.title()}: {question}*"]
        lines.extend(f":{emoji}: {label}" for label, emoji in zip(options, emojis, strict=True))
        ids = await self.bot.post("\n".join(lines), channel_id=task.channel_id, root_ts=task.root_ts)
        if not ids:
            raise McpApiError("poll_create_failed", "Slack did not return a poll message ID")
        message_ts = str(ids[0])
        for emoji in emojis:
            await self.bot.add_reaction(task.channel_id, message_ts, emoji)
        self._polls[message_ts] = {
            "task_id": task_id, "question": question, "options": options,
            "emojis": emojis, "kind": kind,
        }
        return {"task_id": task_id, "message_ts": message_ts, "kind": kind,
                "question": question, "options": [
                    {"label": label, "emoji": emoji}
                    for label, emoji in zip(options, emojis, strict=True)
                ], "created": True}

    async def _create_poll(self, ctx: McpContext, args: Mapping[str, Any]) -> dict[str, Any]:
        return await self._create_poll_common(ctx, args, kind="poll")

    async def _create_approval(self, ctx: McpContext, args: Mapping[str, Any]) -> dict[str, Any]:
        values = dict(args)
        values["options"] = ["Approve", "Reject"]
        values["emojis"] = ["white_check_mark", "x"]
        return await self._create_poll_common(ctx, values, kind="approval")

    async def _get_poll_results(self, ctx: McpContext, args: Mapping[str, Any]) -> dict[str, Any]:
        task_id = self._text(args, "task_id", limit=80)
        message_ts = self._text(args, "message_ts", limit=40)
        task = self._owned_task(ctx, task_id)
        poll = self._polls.get(message_ts)
        if poll is None or poll.get("task_id") != task_id:
            raise McpApiError("poll_not_in_task", "Poll was not created for this task by the current bridge process")
        cursor: str | None = None
        message: Mapping[str, Any] | None = None
        for _ in range(5):
            page = await self.bot.fetch_thread_replies(task.channel_id, task.root_ts, cursor=cursor, limit=100)
            message = next((item for item in page.get("messages") or []
                            if isinstance(item, Mapping) and str(item.get("ts") or "") == message_ts), None)
            if message is not None:
                break
            cursor = self._next_cursor(page)
            if not cursor:
                break
        if message is None:
            raise McpApiError("poll_not_found", "Tracked poll message is no longer in the task thread")
        counts = {str(item.get("name") or ""): max(0, int(item.get("count") or 0) - 1)
                  for item in message.get("reactions") or [] if isinstance(item, Mapping)}
        results = [
            {"label": label, "emoji": emoji, "votes": counts.get(emoji, 0)}
            for label, emoji in zip(poll["options"], poll["emojis"], strict=True)
        ]
        return {"task_id": task_id, "message_ts": message_ts,
                "kind": poll["kind"], "question": poll["question"],
                "results": results, "bot_seed_reaction_excluded": True}

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

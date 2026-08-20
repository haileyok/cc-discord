"""Async HTTP client for a single Polytoken daemon session.

Each Polytoken session is a daemon process bound to a loopback port
(``http://127.0.0.1:<port>``). The daemon is unauthenticated and reachable
only over loopback. This client wraps the per-session endpoints the bridge
needs: prompting, the ``/events`` SSE stream, history/state reads,
interrogative responses, and the lifecycle verbs.

Built on ``aiohttp`` so it shares the bridge's single asyncio event loop and
existing HTTP stack (no extra dependency).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from bridge.redaction import safe_error

log = logging.getLogger(__name__)

# Per-request timeout for ordinary (non-streaming) calls. The /events stream
# uses its own no-read-deadline timeout because it is long-lived.
_DEFAULT_TIMEOUT_SECS = 30.0


class PolytokenClientError(Exception):
    """Base error for daemon HTTP failures.

    ``status`` is the HTTP status code when the daemon answered, or ``None``
    for transport-level failures (connection refused, reset, timeout).
    """

    def __init__(self, message: str, *, status: int | None = None, body: str | None = None) -> None:
        # Never retain provider response bodies: they may contain prompts,
        # credentials, paths, or upstream diagnostics.
        super().__init__(message)
        self.status = status
        self.body = None


class TurnInFlight(PolytokenClientError):
    """Raised on ``POST /prompt`` 409 — a turn is already running."""


class PromptDenied(PolytokenClientError):
    """Raised on ``POST /prompt`` 422 — a pre-prompt hook denied the prompt."""


@dataclass(frozen=True)
class SseEnvelope:
    """One framed ``/events`` message.

    ``seq`` is a monotonic per-session sequence number; it is ``None`` only on
    a synthesized ``stream_discontinuity`` envelope. ``event`` is the raw
    ``DaemonEvent`` dict (a ``{"type": ..., ...}`` object).
    """

    seq: int | None
    session_id: str | None
    emitted_at: str | None
    event: dict[str, Any]

    @property
    def event_type(self) -> str:
        return self.event.get("type", "")

    @property
    def subagent_handle(self) -> str | None:
        """The routing key: set => subagent activity; unset => main session."""
        return self.event.get("subagent_handle")


@dataclass(frozen=True)
class PromptAccepted:
    """Body of the 202 from ``POST /prompt``."""

    prompt_id: str
    session_id: str
    resolved_references: list[dict[str, Any]] = field(default_factory=list)


class PolytokenClient:
    """HTTP client bound to one daemon session at ``http://<host>:<port>``.

    Pass a shared ``aiohttp.ClientSession`` to pool connections across many
    per-task clients; otherwise the client lazily owns its own session and
    closes it in :meth:`aclose`.
    """

    def __init__(
        self,
        port: int,
        *,
        host: str = "127.0.0.1",
        session: aiohttp.ClientSession | None = None,
        timeout_secs: float = _DEFAULT_TIMEOUT_SECS,
    ) -> None:
        self.port = port
        self.host = host
        self._base = f"http://{host}:{port}"
        self._session = session
        self._owns_session = session is None
        self._timeout = aiohttp.ClientTimeout(total=timeout_secs)

    # -- session plumbing -------------------------------------------------

    def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
            self._owns_session = True
        return self._session

    async def aclose(self) -> None:
        if self._owns_session and self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def __aenter__(self) -> "PolytokenClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    def _url(self, path: str) -> str:
        return f"{self._base}{path}"

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """Issue a request and return parsed JSON (or ``None`` for empty bodies).

        Maps daemon error statuses to typed exceptions. Transport failures
        surface as :class:`PolytokenClientError` with ``status=None``.
        """
        session = self._ensure_session()
        try:
            async with session.request(
                method,
                self._url(path),
                json=json_body,
                params=params,
                timeout=self._timeout,
            ) as resp:
                text = await resp.text()
                if resp.status >= 400:
                    self._raise_for_status(method, path, resp.status, text)
                if not text:
                    return None
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return text
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            log.warning("Polytoken %s %s transport failure: %s", method, path, safe_error(exc, "transport failure"))
            raise PolytokenClientError("Polytoken daemon transport failure", status=None)

    @staticmethod
    def _raise_for_status(method: str, path: str, status: int, body: str) -> None:
        if status == 409 and path == "/prompt":
            raise TurnInFlight("a turn is already in flight", status=status)
        if status == 422 and path == "/prompt":
            raise PromptDenied("prompt denied by a pre-prompt hook", status=status)
        raise PolytokenClientError(
            "Polytoken daemon rejected the request", status=status
        )

    # -- prompting --------------------------------------------------------

    async def prompt(self, content: str, *, max_tool_turns: int | None = None) -> PromptAccepted:
        """Submit a prompt. References are embedded in ``content`` as ``@<path>``."""
        body: dict[str, Any] = {"content": content}
        if max_tool_turns is not None:
            body["max_tool_turns"] = max_tool_turns
        data = await self._request("POST", "/prompt", json_body=body)
        data = data or {}
        return PromptAccepted(
            prompt_id=data.get("prompt_id", ""),
            session_id=data.get("session_id", ""),
            resolved_references=data.get("resolved_references") or [],
        )

    # -- reads ------------------------------------------------------------

    async def state(self) -> dict[str, Any]:
        """``GET /state`` -> SessionStateSnapshot. All fields are optional."""
        return await self._request("GET", "/state") or {}

    async def history(self, *, offset: int = 0, limit: int = 200) -> dict[str, Any]:
        """``GET /history?offset&limit`` -> SessionHistorySnapshot."""
        return await self._request(
            "GET", "/history", params={"offset": offset, "limit": limit}
        ) or {}

    async def subagent_history(self, handle: str, *, offset: int = 0, limit: int = 200) -> dict[str, Any]:
        return await self._request(
            "GET", f"/subagent/{handle}/history", params={"offset": offset, "limit": limit}
        ) or {}

    async def jobs(self) -> dict[str, Any]:
        return await self._request("GET", "/jobs") or {}

    async def job(self, handle: str) -> dict[str, Any]:
        return await self._request("GET", f"/job/{handle}") or {}

    # -- interrogatives ---------------------------------------------------

    async def respond_interrogative(self, interrogative_id: str, response: dict[str, Any]) -> Any:
        """``POST /interrogative/{id}/respond`` with an InterrogativeResponse body.

        ``response`` is a ``{"kind": ...}`` object, e.g.
        ``{"kind": "clarification_choice", "choice": "..."}`` or
        ``{"kind": "ask_user_question_answers", "answers": [...]}``.
        """
        return await self._request(
            "POST", f"/interrogative/{interrogative_id}/respond", json_body=response
        )

    # -- lifecycle --------------------------------------------------------

    async def set_title(self, title: str) -> Any:
        return await self._request("POST", "/title", json_body={"title": title})

    async def set_model(self, model: str, *, reasoning_effort: str | None = None) -> Any:
        body: dict[str, Any] = {"model": model}
        if reasoning_effort is not None:
            body["reasoning_effort"] = reasoning_effort
        return await self._request("POST", "/model", json_body=body)

    async def set_facet(self, facet: str) -> Any:
        return await self._request("POST", "/facet", json_body={"facet": facet})

    async def cancel_turn(self) -> Any:
        return await self._request("POST", "/turn/cancel")

    async def compact(self) -> Any:
        return await self._request("POST", "/compact")

    async def clear(self) -> Any:
        return await self._request("POST", "/clear")

    async def terminate(self) -> Any:
        return await self._request("POST", "/terminate")

    async def health(self) -> bool:
        """Return True if ``GET /health`` answers 2xx, False on any failure."""
        try:
            await self._request("GET", "/health")
            return True
        except PolytokenClientError:
            return False

    # -- event stream -----------------------------------------------------

    async def stream_events(self, *, last_seq: int | None = None) -> AsyncIterator[SseEnvelope]:
        """Yield :class:`SseEnvelope` from ``GET /events`` (SSE).

        On reconnect, pass the last seen ``seq`` as ``last_seq``; it is sent
        as the ``Last-Event-ID`` header so the daemon resumes after that
        sequence. The stream is long-lived: callers iterate until the
        connection drops, then reconnect (reconciling via :meth:`history` if
        a ``stream_discontinuity`` is observed).
        """
        headers = {"Accept": "text/event-stream"}
        if last_seq is not None:
            headers["Last-Event-ID"] = str(last_seq)
        # No total/read deadline: the stream stays open between events. A
        # connect deadline still guards against an unreachable daemon.
        stream_timeout = aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=None)
        session = self._ensure_session()
        try:
            async with session.get(
                self._url("/events"), headers=headers, timeout=stream_timeout
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise PolytokenClientError(
                        "Polytoken event stream rejected the request", status=resp.status
                    )
                data_lines: list[str] = []
                async for raw in resp.content:
                    line = raw.decode("utf-8", "replace").rstrip("\n").rstrip("\r")
                    if line == "":
                        # Dispatch on blank line (end of one SSE event).
                        if data_lines:
                            env = self._parse_envelope("\n".join(data_lines))
                            if env is not None:
                                yield env
                            data_lines = []
                        continue
                    if line.startswith(":"):
                        continue  # SSE comment / keep-alive
                    if line.startswith("data:"):
                        data_lines.append(line[5:].lstrip(" "))
                    # `id:` / `event:` fields are redundant with the envelope
                    # body (seq lives inside the JSON); ignore them.
                # Flush a trailing event with no final blank line.
                if data_lines:
                    env = self._parse_envelope("\n".join(data_lines))
                    if env is not None:
                        yield env
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            log.warning("Polytoken event stream transport failure: %s", safe_error(exc, "transport failure"))
            raise PolytokenClientError("Polytoken event stream transport failure", status=None)

    @staticmethod
    def _parse_envelope(data: str) -> SseEnvelope | None:
        try:
            obj = json.loads(data)
        except json.JSONDecodeError:
            log.warning("dropping unparseable Polytoken SSE data frame")
            return None
        event = obj.get("event")
        if not isinstance(event, dict):
            log.warning("Polytoken SSE envelope missing event object")
            return None
        return SseEnvelope(
            seq=obj.get("seq"),
            session_id=obj.get("session_id"),
            emitted_at=obj.get("emitted_at"),
            event=event,
        )

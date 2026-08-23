"""Translate a Polytoken ``DaemonEvent`` stream into Slack render actions.

This is a *pure* translation layer: it consumes :class:`SseEnvelope`s and
produces a list of :class:`Action` intents. It holds only the per-session
state needed to translate (content-block buffers, pending tool calls, the
last seen ``seq``); it never touches Slack. ``TaskRegistry`` interprets the
actions by driving the tool-summary aggregator, subagent Block Kit messages,
and ``Bot`` posting.

Routing rule: every event optionally carries ``subagent_handle``. When set,
the activity belongs to a spawned subagent and is routed to that subagent's
live Block Kit message; when unset, it belongs to the main session and is
routed to the main root-message aggregator. This replaces the old JSONL
``_is_sidechain_tool`` heuristic.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from bridge import tool_summary
from bridge.redaction import safe_error
from bridge.polytoken_client import SseEnvelope

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Action intents. TaskRegistry pattern-matches on these.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Action:
    """Base class for render intents."""


@dataclass(frozen=True)
class AssistantText(Action):
    text: str
    subagent_handle: str | None = None


@dataclass(frozen=True)
class AssistantThinking(Action):
    text: str
    subagent_handle: str | None = None


@dataclass(frozen=True)
class ToolLine(Action):
    """One-liner for the main thread aggregator."""

    line: str


@dataclass(frozen=True)
class ToolDiff(Action):
    """Fenced diff / content / checklist block, posted as its own message."""

    block: str


@dataclass(frozen=True)
class ToolFailure(Action):
    """A failed tool call surfaced on the main thread."""

    line: str


@dataclass(frozen=True)
class SubagentStarted(Action):
    handle: str
    subagent_type: str
    model: str


@dataclass(frozen=True)
class SubagentActivity(Action):
    """A tool one-liner attributed to a running subagent message."""

    handle: str
    line: str


@dataclass(frozen=True)
class PlanHandoff(Action):
    """A `plan_handoff` interrogative: the plan facet asking the operator to
    approve, reject, or refuse-with-feedback a plan before implementation.

    Mirrors the daemon's `PlanHandoffContext` (see `polytoken openapi`):
    ``plan_text`` is the full plan document, ``action_labels`` carries the
    daemon's own presentation strings for each decision so non-TUI clients
    can render the same options without hardcoding copy.
    """

    interrogative_id: str
    prompt_id: str | None
    plan_text: str
    plan_path: str
    display_path: str
    target_facet: str
    title: str
    action_labels: dict[str, str]
    subagent_handle: str | None = None


@dataclass(frozen=True)
class GoalProposal(Action):
    """A `goal_proposal` interrogative: the daemon asking the operator to
    accept or reject tracking a saved-session goal (see `GoalProposalContext`
    in `polytoken openapi`). Approval is binary (`accepted`/`rejected`)."""

    interrogative_id: str
    prompt_id: str | None
    title: str
    proposed_summary: str
    proposed_file_path: str | None
    action_labels: dict[str, str]
    subagent_handle: str | None = None


@dataclass(frozen=True)
class SubagentCompleted(Action):
    handle: str
    outcome_kind: str
    message: str | None
    result_summary: str


@dataclass(frozen=True)
class AskQuestion(Action):
    """Structured ``ask_user_question`` with an untrusted payload isolated."""

    interrogative_id: str
    prompt_id: str | None
    payload: dict[str, Any]
    subagent_handle: str | None = None
    target_actor_id: str | None = None


@dataclass(frozen=True)
class Clarification(Action):
    interrogative_id: str
    prompt_id: str | None
    question: str
    options: list[dict[str, str]] = field(default_factory=list)


@dataclass(frozen=True)
class Confirmation(Action):
    interrogative_id: str
    prompt_id: str | None
    question: str


@dataclass(frozen=True)
class TurnStarted(Action):
    prompt_id: str | None


@dataclass(frozen=True)
class TurnComplete(Action):
    prompt_id: str | None


@dataclass(frozen=True)
class TurnCancelled(Action):
    reason: str


@dataclass(frozen=True)
class ModelError(Action):
    error: str


@dataclass(frozen=True)
class StateRefresh(Action):
    """A ``session_state_changed`` — caller should re-read ``/state``."""

    domains: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class TitleChange(Action):
    title: str


@dataclass(frozen=True)
class CompactionUpdate(Action):
    stage: str
    reason: str | None = None
    detail: str | None = None
    compaction_id: str | None = None


@dataclass(frozen=True)
class ContextCleared(Action):
    pass


@dataclass(frozen=True)
class StatusNote(Action):
    """A small informational note (model/facet switch, compaction, etc.)."""

    text: str


@dataclass(frozen=True)
class AttentionPing(Action):
    summary: str


@dataclass(frozen=True)
class ImageResolved(Action):
    path: str
    media_type: str


@dataclass(frozen=True)
class Reconcile(Action):
    """A gap was detected; caller should reconcile via ``/history``."""

    reason: str


# --------------------------------------------------------------------------
# Tool-name adapter: map Polytoken tool calls onto tool_summary's shapes.
# --------------------------------------------------------------------------

# Polytoken core tool name -> Claude-style name that tool_summary understands.
_TOOL_NAME_MAP = {
    "file_read": "Read",
    "file_write": "Write",
    "file_edit_search_replace": "Edit",
    "shell_exec": "Bash",
    "shell_monitor": "Bash",
    "grep": "Grep",
    "glob": "Glob",
    "web_search": "WebSearch",
    "web_fetch": "WebFetch",
    "subagent": "Task",
    "skill": "Skill",
}


def _adapt_tool(name: str, tool_input: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Return ``(claude_name, claude_input)`` for tool_summary.

    Remaps the few field names that differ between Polytoken and Claude Code
    tool schemas so the existing summarizer renders sensible one-liners.
    Unknown tools pass through unchanged (tool_summary's generic fallback
    handles them).
    """
    claude_name = _TOOL_NAME_MAP.get(name, name)
    ti = dict(tool_input or {})
    # Path-bearing tools: Polytoken uses `path`; Claude uses `file_path`.
    if claude_name in ("Read", "Write", "Edit") and "file_path" not in ti and "path" in ti:
        ti["file_path"] = ti["path"]
    if claude_name == "Bash" and "command" not in ti:
        # shell_monitor uses `command` already; nothing to remap, but guard.
        ti.setdefault("command", ti.get("cmd", ""))
    if claude_name == "WebSearch" and "query" not in ti:
        ti.setdefault("query", ti.get("q", ""))
    if claude_name == "Task" and "description" not in ti:
        ti.setdefault("description", ti.get("prompt", "")[:80] if ti.get("prompt") else "")
    if claude_name == "Skill" and "skill" not in ti:
        ti.setdefault("skill", ti.get("name", ""))
    return claude_name, ti


def _tool_response(result_event: dict[str, Any]) -> dict[str, Any]:
    """Build a tool_summary-compatible ``tool_response`` from a tool_result."""
    resp: dict[str, Any] = {}
    if result_event.get("is_error"):
        resp["is_error"] = True
    content = result_event.get("content")
    if isinstance(content, str) and content:
        resp["error" if result_event.get("is_error") else "stdout"] = content
    return resp


# --------------------------------------------------------------------------
# Translator
# --------------------------------------------------------------------------


@dataclass
class _Block:
    kind: str
    parts: list[str] = field(default_factory=list)
    subagent_handle: str | None = None


@dataclass
class _PendingTool:
    name: str
    tool_input: dict[str, Any]
    subagent_handle: str | None


class Translator:
    """Stateful per-session translator from envelopes to actions."""

    def __init__(self) -> None:
        self._blocks: dict[tuple[int, str | None], _Block] = {}
        self._pending_tools: dict[str, _PendingTool] = {}
        self.last_seq: int | None = None

    # -- seq tracking -----------------------------------------------------

    def _check_seq(self, env: SseEnvelope) -> list[Action]:
        """Detect a sequence gap and emit a Reconcile if one is found."""
        actions: list[Action] = []
        if env.seq is None:
            return actions  # synthesized envelope (e.g. stream_discontinuity)
        if self.last_seq is not None and env.seq > self.last_seq + 1:
            actions.append(
                Reconcile(reason=f"seq gap {self.last_seq}->{env.seq}")
            )
        self.last_seq = max(env.seq, self.last_seq) if self.last_seq is not None else env.seq
        return actions

    # -- entry point ------------------------------------------------------

    def handle(self, env: SseEnvelope) -> list[Action]:
        # Reconnects may replay the boundary event. Never dispatch duplicates or
        # out-of-order envelopes twice; lifecycle actions are not all idempotent.
        if env.seq is not None and self.last_seq is not None and env.seq <= self.last_seq:
            return []
        actions = self._check_seq(env)
        try:
            actions.extend(self._dispatch(env.event))
        except Exception:  # never let one bad event kill the stream consumer
            log.exception("translator failed on event %r", env.event_type)
        return actions

    def _dispatch(self, event: dict[str, Any]) -> list[Action]:
        etype = event.get("type", "")
        handler = getattr(self, f"_on_{etype}", None)
        if handler is None:
            return []
        return handler(event)

    # -- streaming content ------------------------------------------------

    def _on_content_block_start(self, e: dict[str, Any]) -> list[Action]:
        block_type = e.get("block_type") or {}
        kind = block_type.get("type", "text") if isinstance(block_type, dict) else "text"
        key = (e.get("block_index", 0), e.get("subagent_handle"))
        self._blocks[key] = _Block(kind=kind, subagent_handle=e.get("subagent_handle"))
        return []

    def _on_content_block_delta(self, e: dict[str, Any]) -> list[Action]:
        key = (e.get("block_index", 0), e.get("subagent_handle"))
        block = self._blocks.get(key)
        delta = e.get("delta") or {}
        dtype = delta.get("type")
        if block is None:
            # Missed the start; infer kind from the delta.
            kind = "thinking" if dtype == "thinking" else "text"
            block = _Block(kind=kind, subagent_handle=e.get("subagent_handle"))
            self._blocks[key] = block
        if dtype in ("text", "thinking"):
            block.parts.append(delta.get("text", ""))
        # tool_use_input partial_json is ignored; the full tool_call event carries input.
        return []

    def _on_content_block_stop(self, e: dict[str, Any]) -> list[Action]:
        key = (e.get("block_index", 0), e.get("subagent_handle"))
        block = self._blocks.pop(key, None)
        if block is None:
            return []
        text = "".join(block.parts).strip()
        if not text:
            return []
        if block.kind == "thinking":
            return [AssistantThinking(text=text, subagent_handle=block.subagent_handle)]
        if block.kind == "text":
            return [AssistantText(text=text, subagent_handle=block.subagent_handle)]
        return []

    def _on_message_start(self, e: dict[str, Any]) -> list[Action]:
        return [TurnStarted(prompt_id=e.get("prompt_id"))]

    def _on_message_complete(self, e: dict[str, Any]) -> list[Action]:
        return [TurnComplete(prompt_id=e.get("prompt_id"))]

    def _on_turn_cancelled(self, e: dict[str, Any]) -> list[Action]:
        return [TurnCancelled(reason=e.get("reason", ""))]

    def _on_model_error(self, e: dict[str, Any]) -> list[Action]:
        # Provider/model payloads may contain URLs, prompts, credentials, or
        # filesystem paths. Never forward the raw daemon error to Slack.
        return [ModelError(error=safe_error(None, "The daemon reported a model error"))]

    @staticmethod
    def _compaction_reason(e: dict[str, Any]) -> str | None:
        reason = e.get("reason")
        if isinstance(reason, dict):
            reason = reason.get("type") or reason.get("kind")
        return str(reason).strip()[:80] if reason else None

    @staticmethod
    def _compaction_id(e: dict[str, Any]) -> str | None:
        value = e.get("compaction_id")
        return value if isinstance(value, str) and value else None

    def _on_compaction_started(self, e: dict[str, Any]) -> list[Action]:
        return [CompactionUpdate("started", self._compaction_reason(e), compaction_id=self._compaction_id(e))]

    def _on_compaction_retry(self, e: dict[str, Any]) -> list[Action]:
        attempt = e.get("attempt")
        maximum = e.get("max_attempts") or e.get("total_attempts")
        detail = f"attempt {attempt}/{maximum}" if attempt and maximum else "retrying"
        return [CompactionUpdate("retry", self._compaction_reason(e), detail, self._compaction_id(e))]

    def _on_compaction_complete(self, e: dict[str, Any]) -> list[Action]:
        return [CompactionUpdate("complete", self._compaction_reason(e), compaction_id=self._compaction_id(e))]

    def _on_compaction_failed(self, e: dict[str, Any]) -> list[Action]:
        return [CompactionUpdate("failed", self._compaction_reason(e), compaction_id=self._compaction_id(e))]

    def _on_compaction_cancelled(self, e: dict[str, Any]) -> list[Action]:
        return [CompactionUpdate("cancelled", self._compaction_reason(e), compaction_id=self._compaction_id(e))]

    def _on_context_cleared(self, e: dict[str, Any]) -> list[Action]:
        return [ContextCleared()]

    # -- tools ------------------------------------------------------------

    def _on_tool_call(self, e: dict[str, Any]) -> list[Action]:
        call_id = e.get("call_id", "")
        self._pending_tools[call_id] = _PendingTool(
            name=e.get("name", "?"),
            tool_input=e.get("input") if isinstance(e.get("input"), dict) else {},
            subagent_handle=e.get("subagent_handle"),
        )
        return []

    def _on_tool_result(self, e: dict[str, Any]) -> list[Action]:
        call_id = e.get("call_id", "")
        pending = self._pending_tools.pop(call_id, None)
        if pending is None:
            return []
        claude_name, claude_input = _adapt_tool(pending.name, pending.tool_input)
        response = _tool_response(e)
        line = tool_summary.summarize(claude_name, claude_input, response)
        diff = tool_summary.diff_block(claude_name, claude_input)
        failed = tool_summary.is_failure(response, claude_name)

        if pending.subagent_handle:
            # Subagent activity routes to its Block Kit message (one compact line).
            return [SubagentActivity(handle=pending.subagent_handle, line=line)]

        actions: list[Action] = []
        if failed:
            actions.append(ToolFailure(line=line))
        else:
            actions.append(ToolLine(line=line))
        if diff:
            actions.append(ToolDiff(block=diff))
        return actions

    # -- subagents --------------------------------------------------------

    def _on_subagent_started(self, e: dict[str, Any]) -> list[Action]:
        return [
            SubagentStarted(
                handle=e.get("handle", ""),
                subagent_type=e.get("subagent_type", ""),
                model=e.get("model", ""),
            )
        ]

    def _on_subagent_completed(self, e: dict[str, Any]) -> list[Action]:
        outcome = e.get("outcome") or {}
        return [
            SubagentCompleted(
                handle=e.get("handle", ""),
                outcome_kind=str(outcome.get("kind", "")),
                message=outcome.get("message"),
                result_summary=e.get("result_summary", ""),
            )
        ]

    # -- interrogatives ---------------------------------------------------

    def _on_ask_user_question(self, e: dict[str, Any]) -> list[Action]:
        return [
            AskQuestion(
                interrogative_id=e.get("interrogative_id", ""),
                prompt_id=e.get("prompt_id"),
                payload=e.get("payload") if isinstance(e.get("payload"), dict) else {},
                subagent_handle=e.get("subagent_handle"),
                target_actor_id=str((e.get("payload") or {}).get("actor_id") or (e.get("payload") or {}).get("target_actor_id") or "") or None,
            )
        ]

    def _on_interrogative(self, e: dict[str, Any]) -> list[Action]:
        itype = e.get("interrogative_type")
        iid = e.get("interrogative_id", "")
        pid = e.get("prompt_id")
        question = str(e.get("question", ""))[:4000]
        if itype == "permission":
            # Bypass mode: permission interrogatives never need a user answer.
            return []
        if itype == "clarification":
            opts = e.get("clarification_options") or []
            options = [
                {"key": o.get("key", ""), "label": o.get("label", "")}
                for o in opts
                if isinstance(o, dict)
            ]
            return [Clarification(interrogative_id=iid, prompt_id=pid, question=question, options=options)]
        if itype == "confirmation":
            return [Confirmation(interrogative_id=iid, prompt_id=pid, question=question)]
        if itype == "plan_handoff":
            ph = e.get("plan_handoff") if isinstance(e.get("plan_handoff"), dict) else {}
            labels = ph.get("action_labels")
            return [
                PlanHandoff(
                    interrogative_id=iid,
                    prompt_id=pid,
                    plan_text=str(ph.get("plan_text", "")),
                    plan_path=str(ph.get("plan_path", "")),
                    display_path=str(ph.get("display_path", "")),
                    target_facet=str(ph.get("target_facet", "")),
                    title=str(ph.get("title") or question or "Approve plan?"),
                    action_labels={str(k): str(v) for k, v in labels.items()} if isinstance(labels, dict) else {},
                    subagent_handle=e.get("subagent_handle"),
                )
            ]
        if itype == "goal_proposal":
            gp = e.get("goal_proposal") if isinstance(e.get("goal_proposal"), dict) else {}
            labels = gp.get("action_labels")
            return [
                GoalProposal(
                    interrogative_id=iid,
                    prompt_id=pid,
                    title=str(gp.get("title") or question or "Accept this goal?"),
                    proposed_summary=str(gp.get("proposed_summary", "")),
                    proposed_file_path=gp.get("proposed_file_path") if isinstance(gp.get("proposed_file_path"), str) else None,
                    action_labels={str(k): str(v) for k, v in labels.items()} if isinstance(labels, dict) else {},
                    subagent_handle=e.get("subagent_handle"),
                )
            ]
        # capability: surface as a clarification-less note for now.
        return [StatusNote(text=f"❓ {question}")] if question else []

    # -- session / lifecycle ---------------------------------------------

    def _on_session_state_changed(self, e: dict[str, Any]) -> list[Action]:
        domains = e.get("domains") or []
        return [StateRefresh(domains=[str(d) for d in domains])]

    def _on_session_title_changed(self, e: dict[str, Any]) -> list[Action]:
        return [TitleChange(title=e.get("title", ""))]

    def _on_model_switch(self, e: dict[str, Any]) -> list[Action]:
        return [StatusNote(text=f"🔧 model → {e.get('to_model', '?')}")]

    def _on_facet_switch(self, e: dict[str, Any]) -> list[Action]:
        return [StatusNote(text=f"🎭 facet → {e.get('to_facet', '?')}")]

    def _on_notification_queued(self, e: dict[str, Any]) -> list[Action]:
        note = e.get("notification") or {}
        return [AttentionPing(summary=note.get("summary", "needs your attention"))]

    def _on_image_reference_resolved(self, e: dict[str, Any]) -> list[Action]:
        return [ImageResolved(path=e.get("path", ""), media_type=e.get("media_type", ""))]

    def _on_stream_discontinuity(self, e: dict[str, Any]) -> list[Action]:
        return [Reconcile(reason=f"stream_discontinuity missed={e.get('missed')}")]

"""Tests for bridge.events translator (pure envelope -> action mapping)."""

from itertools import count

from bridge.events import (
    AskQuestion,
    AssistantText,
    AssistantThinking,
    AttentionPing,
    Clarification,
    Confirmation,
    ModelError,
    Reconcile,
    StateRefresh,
    StatusNote,
    SubagentActivity,
    SubagentCompleted,
    SubagentStarted,
    TitleChange,
    ToolDiff,
    ToolFailure,
    ToolLine,
    Translator,
    TurnComplete,
    TurnStarted,
)
from bridge.polytoken_client import SseEnvelope


class _Feed:
    """Build envelopes with auto-incrementing seq and drive a Translator."""

    def __init__(self) -> None:
        self.t = Translator()
        self._seq = count()

    def send(self, event: dict, *, seq: int | None = None):
        s = next(self._seq) if seq is None else seq
        return self.t.handle(SseEnvelope(seq=s, session_id="s", emitted_at=None, event=event))


def _types(actions):
    return [type(a) for a in actions]


class TestStreaming:
    def test_text_block_accumulates_and_emits_on_stop(self) -> None:
        f = _Feed()
        f.send({"type": "content_block_start", "block_index": 0, "block_type": {"type": "text"}})
        assert f.send({"type": "content_block_delta", "block_index": 0,
                       "delta": {"type": "text", "text": "Hello "}}) == []
        f.send({"type": "content_block_delta", "block_index": 0,
                "delta": {"type": "text", "text": "world"}})
        out = f.send({"type": "content_block_stop", "block_index": 0})
        assert out == [AssistantText(text="Hello world", subagent_handle=None)]

    def test_thinking_block(self) -> None:
        f = _Feed()
        f.send({"type": "content_block_start", "block_index": 0, "block_type": {"type": "thinking"}})
        f.send({"type": "content_block_delta", "block_index": 0,
                "delta": {"type": "thinking", "text": "hmm"}})
        out = f.send({"type": "content_block_stop", "block_index": 0})
        assert out == [AssistantThinking(text="hmm", subagent_handle=None)]

    def test_empty_block_emits_nothing(self) -> None:
        f = _Feed()
        f.send({"type": "content_block_start", "block_index": 0, "block_type": {"type": "text"}})
        assert f.send({"type": "content_block_stop", "block_index": 0}) == []

    def test_message_start_complete(self) -> None:
        f = _Feed()
        assert f.send({"type": "message_start", "prompt_id": "p"}) == [TurnStarted(prompt_id="p")]
        assert f.send({"type": "message_complete", "prompt_id": "p"}) == [TurnComplete(prompt_id="p")]

    def test_model_error(self) -> None:
        f = _Feed()
        assert _types(f.send({"type": "model_error", "prompt_id": "p", "error": "boom"})) == [ModelError]


class TestTools:
    def test_tool_call_result_pair_emits_line(self) -> None:
        f = _Feed()
        assert f.send({"type": "tool_call", "prompt_id": "p", "call_id": "c1",
                       "name": "shell_exec", "input": {"command": "ls -la"}}) == []
        out = f.send({"type": "tool_result", "prompt_id": "p", "call_id": "c1", "content": "ok"})
        assert _types(out) == [ToolLine]
        assert "Bash" in out[0].line and "ls -la" in out[0].line

    def test_edit_emits_line_and_diff(self) -> None:
        f = _Feed()
        f.send({"type": "tool_call", "prompt_id": "p", "call_id": "c2", "name": "file_edit_search_replace",
                "input": {"path": "/a/b.py", "old_string": "x", "new_string": "y"}})
        out = f.send({"type": "tool_result", "prompt_id": "p", "call_id": "c2"})
        assert _types(out) == [ToolLine, ToolDiff]
        assert "b.py" in out[0].line

    def test_failed_tool_emits_failure(self) -> None:
        f = _Feed()
        f.send({"type": "tool_call", "prompt_id": "p", "call_id": "c3", "name": "shell_exec",
                "input": {"command": "false"}})
        out = f.send({"type": "tool_result", "prompt_id": "p", "call_id": "c3",
                      "content": "boom", "is_error": True})
        assert _types(out) == [ToolFailure]

    def test_orphan_tool_result_ignored(self) -> None:
        f = _Feed()
        assert f.send({"type": "tool_result", "prompt_id": "p", "call_id": "nope"}) == []

    def test_subagent_tool_routes_to_activity(self) -> None:
        f = _Feed()
        f.send({"type": "tool_call", "prompt_id": "p", "call_id": "c4", "name": "file_read",
                "input": {"path": "/x"}, "subagent_handle": "agent-1"})
        out = f.send({"type": "tool_result", "prompt_id": "p", "call_id": "c4",
                      "subagent_handle": "agent-1"})
        assert _types(out) == [SubagentActivity]
        assert out[0].handle == "agent-1"


class TestSubagents:
    def test_started_completed(self) -> None:
        f = _Feed()
        assert f.send({"type": "subagent_started", "handle": "h1", "subagent_type": "researcher",
                       "model": "m"}) == [SubagentStarted("h1", "researcher", "m")]
        out = f.send({"type": "subagent_completed", "handle": "h1",
                      "outcome": {"kind": "success", "message": "done"}, "result_summary": "ok"})
        assert out == [SubagentCompleted("h1", "success", "done", "ok")]


class TestInterrogatives:
    def test_ask_user_question(self) -> None:
        f = _Feed()
        out = f.send({"type": "ask_user_question", "prompt_id": "p", "interrogative_id": "i1",
                      "payload": {"questions": [{"question": "q?"}]}})
        assert _types(out) == [AskQuestion]
        assert out[0].interrogative_id == "i1"

    def test_clarification(self) -> None:
        f = _Feed()
        out = f.send({"type": "interrogative", "prompt_id": "p", "interrogative_id": "i2",
                      "question": "pick", "interrogative_type": "clarification",
                      "clarification_options": [{"key": "a", "label": "A"}, {"key": "b", "label": "B"}]})
        assert _types(out) == [Clarification]
        assert out[0].options == [{"key": "a", "label": "A"}, {"key": "b", "label": "B"}]

    def test_confirmation(self) -> None:
        f = _Feed()
        out = f.send({"type": "interrogative", "prompt_id": "p", "interrogative_id": "i3",
                      "question": "sure?", "interrogative_type": "confirmation"})
        assert _types(out) == [Confirmation]

    def test_permission_suppressed_under_bypass(self) -> None:
        f = _Feed()
        out = f.send({"type": "interrogative", "prompt_id": "p", "interrogative_id": "i4",
                      "question": "allow?", "interrogative_type": "permission"})
        assert out == []


class TestSessionAndGaps:
    def test_title_change(self) -> None:
        f = _Feed()
        assert f.send({"type": "session_title_changed", "title": "New"}) == [TitleChange("New")]

    def test_state_refresh(self) -> None:
        f = _Feed()
        assert f.send({"type": "session_state_changed", "domains": ["todos"]}) == [
            StateRefresh(domains=["todos"])
        ]

    def test_model_switch_note(self) -> None:
        f = _Feed()
        assert _types(f.send({"type": "model_switch", "from_model": "a", "to_model": "b"})) == [StatusNote]

    def test_notification_ping(self) -> None:
        f = _Feed()
        out = f.send({"type": "notification_queued", "notification": {"summary": "look"}})
        assert out == [AttentionPing("look")]

    def test_stream_discontinuity_reconcile(self) -> None:
        f = _Feed()
        out = f.t.handle(SseEnvelope(seq=None, session_id="s", emitted_at=None,
                                     event={"type": "stream_discontinuity", "missed": 3}))
        assert _types(out) == [Reconcile]

    def test_seq_gap_triggers_reconcile(self) -> None:
        f = _Feed()
        f.send({"type": "heartbeat", "timestamp": "t"}, seq=0)
        out = f.send({"type": "heartbeat", "timestamp": "t"}, seq=5)
        assert any(isinstance(a, Reconcile) for a in out)

    def test_contiguous_seq_no_reconcile(self) -> None:
        f = _Feed()
        f.send({"type": "heartbeat", "timestamp": "t"}, seq=0)
        out = f.send({"type": "heartbeat", "timestamp": "t"}, seq=1)
        assert not any(isinstance(a, Reconcile) for a in out)

    def test_unknown_event_ignored(self) -> None:
        f = _Feed()
        assert f.send({"type": "totally_new_event_kind", "x": 1}) == []

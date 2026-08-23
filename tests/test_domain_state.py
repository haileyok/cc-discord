"""Focused tests for normalized domain values and Slack state persistence."""

from __future__ import annotations

import aiosqlite
import pytest

from bridge.domain import (
    ActorId,
    ChannelId,
    ConversationKey,
    EventId,
    Mode,
    Owner,
    Participant,
    ParticipantKind,
    PendingInterrogative,
    RootId,
    TextPin,
    TeamId,
)
from bridge import state


def key() -> ConversationKey:
    return ConversationKey(TeamId(" T1 "), ChannelId("C1"), "R1")


class TestDomainValues:
    def test_ids_are_opaque_nonempty_strings(self) -> None:
        assert TeamId(" T1 ") == "T1"
        assert isinstance(TeamId("T1"), str)
        with pytest.raises(ValueError):
            ChannelId("  ")
        with pytest.raises(TypeError):
            RootId(123)  # type: ignore[arg-type]

    def test_owner_and_participant_kinds(self) -> None:
        owner = Owner("U1", ParticipantKind.HUMAN, Mode("shared"))
        app = Participant("A1", ParticipantKind.APP, " helper ")
        assert owner.actor_id == "U1" and owner.kind is ParticipantKind.HUMAN
        assert owner.mode == "shared"
        assert app.actor_id == "A1" and app.kind is ParticipantKind.APP
        assert app.display_name == "helper"

    def test_pending_and_pin_reject_invalid_values(self) -> None:
        with pytest.raises(ValueError):
            PendingInterrogative("i", "U1", {}, expires_at=1, created_at=2)
        with pytest.raises(ValueError):
            TextPin("p", key(), "  ", None, 1, 1)


@pytest.mark.asyncio
class TestNormalizedState:
    async def test_root_participants_and_opaque_ids(self, in_memory_db) -> None:
        root = await state.upsert_root(in_memory_db, key(), Owner("U1"), now=10)
        assert root.owner.actor_id == ActorId("U1")
        assert (await state.get_root(in_memory_db, key())).team_id == "T1"

        await state.upsert_participant(
            in_memory_db, key(), Participant("U1", ParticipantKind.HUMAN), now=11
        )
        await state.upsert_participant(
            in_memory_db, key(), Participant("A1", ParticipantKind.APP), now=12
        )
        participants = await state.list_participants(in_memory_db, key())
        assert [(p.participant.actor_id, p.participant.kind) for p in participants] == [
            ("A1", ParticipantKind.APP),
            ("U1", ParticipantKind.HUMAN),
        ]

    async def test_promotion_keeps_history_and_one_active(self, in_memory_db) -> None:
        first = await state.promote_root(in_memory_db, key(), "TGT1", "channel", now=10)
        second = await state.promote_root(in_memory_db, key(), "TGT2", "channel", now=20)
        assert first.active and second.active
        assert await state.get_active_promotion(in_memory_db, key()) == second
        history = await state.list_promotion_bindings(in_memory_db, key())
        assert len(history) == 2
        assert history[0].active is False and history[0].ended_at == 20

    async def test_dedup_is_first_seen_and_bounded(self, in_memory_db) -> None:
        assert await state.mark_event_seen(in_memory_db, "T1", EventId("E1"), now=1, max_records=2)
        assert not await state.mark_event_seen(in_memory_db, "T1", "E1", now=2, max_records=2)
        assert await state.mark_event_seen(in_memory_db, "T1", "E2", now=3, max_records=2)
        assert await state.mark_event_seen(in_memory_db, "T1", "E3", now=4, max_records=2)
        assert not await state.is_event_seen(in_memory_db, "T1", "E1")
        assert await state.is_event_seen(in_memory_db, "T1", "E3")

    async def test_pending_is_actor_targeted_and_expires(self, in_memory_db) -> None:
        pending = PendingInterrogative("I1", "U1", {"question": "yes?"}, 20, 10)
        await state.put_pending_interrogative(in_memory_db, key(), pending)
        assert await state.get_pending_interrogative(in_memory_db, key(), "U2", now=11) is None
        assert await state.get_pending_interrogative(in_memory_db, key(), "U1", now=11) == pending
        assert await state.get_pending_interrogative(in_memory_db, key(), "U1", now=20) is None

    async def test_text_pins_roundtrip(self, in_memory_db) -> None:
        pin = TextPin("P1", key(), "remember this", ActorId("U1"), 10, 10)
        await state.upsert_text_pin(in_memory_db, pin)
        assert await state.get_text_pin(in_memory_db, key()) == pin
        assert await state.delete_text_pin(in_memory_db, key(), "P1")
        assert await state.get_text_pin(in_memory_db, key()) is None



"""Slack text-channel pin acceptance tests (AC.2/AC.6/AC.7)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from bridge import state
from bridge.domain import ConversationKey
from bridge.tasks import Task, TaskPrivilegeError, TaskRegistry


@dataclass
class PinBot:
    reactions: list[dict[str, Any]] = field(default_factory=list)
    removed_reactions: list[dict[str, Any]] = field(default_factory=list)

    async def add_reaction(self, channel_id: str, message_ts: str, name: str):
        self.reactions.append({"channel_id": channel_id, "message_ts": message_ts, "name": name})

    async def remove_reaction(self, channel_id: str, message_ts: str, name: str):
        self.removed_reactions.append({"channel_id": channel_id, "message_ts": message_ts, "name": name})


@dataclass
class PinSupervisor:
    async def list_models(self):
        return []


@pytest.fixture
def pin_key() -> ConversationKey:
    return ConversationKey("T1", "CHOME", "1000.100")


@pytest.mark.asyncio
class TestTextPins:
    async def test_pin_is_text_root_bound_and_reacts(self, in_memory_db, pin_key):
        bot = PinBot()
        registry = TaskRegistry(in_memory_db, bot, PinSupervisor())
        pin = await registry.pin_channel(pin_key, "remember this", "UOWNER")
        assert pin.key == pin_key
        assert pin.text == "remember this"
        assert bot.reactions == [{"channel_id": "CHOME", "message_ts": "1000.100", "name": "pushpin"}]
        assert await state.get_text_pin(in_memory_db, pin_key) == pin

    async def test_pin_lookup_delete_and_listing_use_conversation_key(self, in_memory_db, pin_key):
        bot = PinBot()
        registry = TaskRegistry(in_memory_db, bot, PinSupervisor())
        first = await registry.pin_channel(pin_key, "one", "UOWNER")
        second = await registry.pin_channel(pin_key, "two", "UOWNER")
        assert await registry.get_pin_for(pin_key, "UOWNER", first.pin_id) == first
        assert {pin.text for pin in await registry.list_all_pins(pin_key, "UOWNER")} == {"one", "two"}
        assert await registry.unpin_channel(pin_key, "UOWNER", first.pin_id)
        assert await registry.get_pin_for(pin_key, "UOWNER", first.pin_id) is None
        assert bot.removed_reactions == []  # another pin still owns the indicator
        assert await registry.unpin_channel(pin_key, "UOWNER", first.pin_id) is False
        assert await registry.unpin_channel(pin_key, "UOWNER", second.pin_id)
        assert bot.removed_reactions == [
            {"channel_id": "CHOME", "message_ts": "1000.100", "name": "pushpin"}
        ]

    async def test_pin_privilege_is_owner_only_when_root_is_bound(self, in_memory_db, pin_key):
        registry = TaskRegistry(in_memory_db, PinBot(), PinSupervisor())
        task = Task("t1", "T1", "CHOME", "1000.100", "UOWNER", "personal", "/tmp", "running")
        await registry.attach_task(task)
        with pytest.raises(TaskPrivilegeError):
            await registry.pin_channel(pin_key, "private", "UOTHER")
        await registry.pin_channel(pin_key, "private", "UOWNER")
        with pytest.raises(TaskPrivilegeError):
            await registry.unpin_channel(pin_key, "UOTHER", (await registry.list_all_pins(pin_key))[0].pin_id)

    async def test_pin_rejects_empty_text_and_requires_normalized_key(self, in_memory_db):
        registry = TaskRegistry(in_memory_db, PinBot(), PinSupervisor())
        with pytest.raises(TypeError):
            await registry.pin_channel(123, "text", "UOWNER")  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            await registry.pin_channel(ConversationKey("T1", "CHOME", "1000.101"), "  ", "UOWNER")

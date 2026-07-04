"""Tests for the pins table CRUD and TaskRegistry pin methods."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from bridge.state import (
    PinRow,
    delete_pin,
    get_pin,
    list_pins,
    touch_pin,
    upsert_pin,
)
from bridge.tasks import TaskRegistry
from tests.fakes import FakeBot, FakeSupervisor as FakeZellij


@pytest.mark.asyncio
class TestPinsStateCRUD:
    """Direct CRUD against the pins table via state.py functions."""

    async def test_get_pin_missing_returns_none(self, in_memory_db) -> None:
        assert await get_pin(in_memory_db, 12345) is None

    async def test_upsert_then_get(self, in_memory_db, tmp_path: Path) -> None:
        await upsert_pin(in_memory_db, 12345, str(tmp_path))
        row = await get_pin(in_memory_db, 12345)
        assert row is not None
        assert row.channel_id == 12345
        assert row.cwd == str(tmp_path)
        assert row.created_at > 0
        assert row.last_used_at == row.created_at

    async def test_upsert_updates_cwd_and_last_used_preserves_created(
        self, in_memory_db, tmp_path: Path
    ) -> None:
        await upsert_pin(in_memory_db, 12345, str(tmp_path), now=1000)
        await upsert_pin(in_memory_db, 12345, str(tmp_path / "x"), now=2000)
        row = await get_pin(in_memory_db, 12345)
        assert row is not None
        assert row.cwd == str(tmp_path / "x")
        assert row.created_at == 1000  # preserved
        assert row.last_used_at == 2000  # bumped

    async def test_touch_bumps_last_used_only(
        self, in_memory_db, tmp_path: Path
    ) -> None:
        await upsert_pin(in_memory_db, 12345, str(tmp_path), now=1000)
        await touch_pin(in_memory_db, 12345, now=5000)
        row = await get_pin(in_memory_db, 12345)
        assert row is not None
        assert row.cwd == str(tmp_path)
        assert row.created_at == 1000
        assert row.last_used_at == 5000

    async def test_touch_missing_is_noop(self, in_memory_db) -> None:
        # Should not raise; no row to update.
        await touch_pin(in_memory_db, 99999, now=1234)
        assert await get_pin(in_memory_db, 99999) is None

    async def test_delete_returns_true_when_present(
        self, in_memory_db, tmp_path: Path
    ) -> None:
        await upsert_pin(in_memory_db, 12345, str(tmp_path))
        assert await delete_pin(in_memory_db, 12345) is True
        assert await get_pin(in_memory_db, 12345) is None

    async def test_delete_returns_false_when_absent(self, in_memory_db) -> None:
        assert await delete_pin(in_memory_db, 99999) is False

    async def test_list_orders_by_last_used_desc(
        self, in_memory_db, tmp_path: Path
    ) -> None:
        await upsert_pin(in_memory_db, 1, str(tmp_path), now=1000)
        await upsert_pin(in_memory_db, 2, str(tmp_path), now=2000)
        await upsert_pin(in_memory_db, 3, str(tmp_path), now=3000)
        rows = await list_pins(in_memory_db)
        assert [r.channel_id for r in rows] == [3, 2, 1]

    async def test_pin_row_is_frozen_dataclass(self, tmp_path: Path) -> None:
        row = PinRow(channel_id=1, cwd=str(tmp_path), created_at=0, last_used_at=0)
        with pytest.raises(Exception):
            row.cwd = "x"  # type: ignore[misc]


@pytest.mark.asyncio
class TestTaskRegistryPinMethods:
    """TaskRegistry's pin_channel / unpin_channel / get_pin_for / list_all_pins."""

    async def test_pin_channel_persists_and_get_returns_it(
        self, in_memory_db, tmp_path: Path
    ) -> None:
        registry = TaskRegistry(in_memory_db, FakeBot(), FakeZellij())
        await registry.pin_channel(12345, str(tmp_path))
        row = await registry.get_pin_for(12345)
        assert row is not None
        assert row.cwd == str(tmp_path)

    async def test_pin_channel_rejects_nonexistent_cwd(
        self, in_memory_db
    ) -> None:
        registry = TaskRegistry(in_memory_db, FakeBot(), FakeZellij())
        with pytest.raises(ValueError, match="does not exist"):
            await registry.pin_channel(12345, "/nope/this/does/not/exist")

    async def test_unpin_channel_returns_true_when_removed(
        self, in_memory_db, tmp_path: Path
    ) -> None:
        registry = TaskRegistry(in_memory_db, FakeBot(), FakeZellij())
        await registry.pin_channel(12345, str(tmp_path))
        assert await registry.unpin_channel(12345) is True
        assert await registry.get_pin_for(12345) is None

    async def test_unpin_channel_returns_false_when_absent(
        self, in_memory_db
    ) -> None:
        registry = TaskRegistry(in_memory_db, FakeBot(), FakeZellij())
        assert await registry.unpin_channel(99999) is False

    async def test_list_all_pins_returns_all(
        self, in_memory_db, tmp_path: Path
    ) -> None:
        registry = TaskRegistry(in_memory_db, FakeBot(), FakeZellij())
        await registry.pin_channel(1, str(tmp_path))
        await registry.pin_channel(2, str(tmp_path))
        pins = await registry.list_all_pins()
        assert {p.channel_id for p in pins} == {1, 2}


@pytest.mark.asyncio
class TestMaybeSpawnForPinned:
    """Auto-spawn behavior on pinned channels via maybe_route_message hook.

    These tests verify the routing decision, not the full spawn flow.
    The full spawn path is exercised by test_tasks.py's TaskRegistry tests.
    """

    async def test_returns_none_when_channel_not_pinned(
        self, in_memory_db
    ) -> None:
        registry = TaskRegistry(in_memory_db, FakeBot(), FakeZellij())
        # Channel 12345 has no pin row.
        result = await registry._maybe_spawn_for_pinned(12345)
        assert result is None

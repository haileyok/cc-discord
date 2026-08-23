"""Slack-facing normalized domain values and storage protocols.

The Slack adapter should translate provider payloads into these small values
before handing them to persistence. IDs intentionally remain opaque strings:
Slack IDs are not numbers and must never be coerced through ``int``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping, Protocol, runtime_checkable


class _StringId(str):
    """A non-empty, whitespace-trimmed opaque provider identifier."""

    label = "identifier"

    def __new__(cls, value: str) -> "_StringId":
        if not isinstance(value, str):
            raise TypeError(f"{cls.label} must be a string")
        value = value.strip()
        if not value:
            raise ValueError(f"{cls.label} must not be empty")
        return str.__new__(cls, value)


class TeamId(_StringId):
    label = "team_id"


class ChannelId(_StringId):
    label = "channel_id"


class RootId(_StringId):
    label = "root_id"


class ActorId(_StringId):
    label = "actor_id"


class EventId(_StringId):
    label = "event_id"


class Mode(_StringId):
    """Opaque conversation mode (for example ``shared`` or ``personal``)."""

    label = "mode"


ConversationMode = Mode


class ParticipantKind(StrEnum):
    HUMAN = "human"
    APP = "app"


@dataclass(frozen=True, slots=True)
class ConversationKey:
    """Stable identity of a Slack root message and its replies."""

    team_id: TeamId
    channel_id: ChannelId
    root_id: RootId

    def __post_init__(self) -> None:
        object.__setattr__(self, "team_id", TeamId(self.team_id))
        object.__setattr__(self, "channel_id", ChannelId(self.channel_id))
        object.__setattr__(self, "root_id", RootId(self.root_id))


RootKey = ConversationKey


@dataclass(frozen=True, slots=True)
class Owner:
    """The actor that owns a root and the ownership mode."""

    actor_id: ActorId
    kind: ParticipantKind = ParticipantKind.HUMAN
    mode: Mode = Mode("shared")

    def __post_init__(self) -> None:
        object.__setattr__(self, "actor_id", ActorId(self.actor_id))
        object.__setattr__(self, "kind", ParticipantKind(self.kind))
        object.__setattr__(self, "mode", Mode(self.mode))


@dataclass(frozen=True, slots=True)
class Participant:
    """A human or app actor participating in a root."""

    actor_id: ActorId
    kind: ParticipantKind
    display_name: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "actor_id", ActorId(self.actor_id))
        object.__setattr__(self, "kind", ParticipantKind(self.kind))
        if self.display_name is not None:
            name = self.display_name.strip()
            object.__setattr__(self, "display_name", name or None)


@dataclass(frozen=True, slots=True)
class PromotionBinding:
    """One historical or active binding produced by promoting a root."""

    binding_id: str
    target_id: str
    target_kind: str
    created_at: int
    ended_at: int | None = None
    promoted_by: ActorId | None = None
    active: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "binding_id", _StringId(self.binding_id))
        object.__setattr__(self, "target_id", _StringId(self.target_id))
        object.__setattr__(self, "target_kind", _StringId(self.target_kind))
        if self.promoted_by is not None:
            object.__setattr__(self, "promoted_by", ActorId(self.promoted_by))
        if self.created_at < 0:
            raise ValueError("created_at must be non-negative")
        if self.ended_at is not None and self.ended_at < self.created_at:
            raise ValueError("ended_at must not precede created_at")


@dataclass(frozen=True, slots=True)
class PendingInterrogative:
    """A question addressed to one actor until its expiry timestamp."""

    interrogative_id: str
    actor_id: ActorId
    payload: Mapping[str, Any]
    expires_at: int
    created_at: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "interrogative_id", _StringId(self.interrogative_id))
        object.__setattr__(self, "actor_id", ActorId(self.actor_id))
        if self.expires_at < self.created_at:
            raise ValueError("expires_at must not precede created_at")


@dataclass(frozen=True, slots=True)
class TextPin:
    """Text pinned to a Slack root; ``pin_id`` is provider/message opaque."""

    pin_id: str
    key: ConversationKey
    text: str
    actor_id: ActorId | None
    created_at: int
    updated_at: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "pin_id", _StringId(self.pin_id))
        if not isinstance(self.key, ConversationKey):
            raise TypeError("key must be a ConversationKey")
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("text pin text must not be empty")
        if self.actor_id is not None:
            object.__setattr__(self, "actor_id", ActorId(self.actor_id))


@runtime_checkable
class NormalizedMessage(Protocol):
    """Minimum provider-neutral message shape needed by a Slack adapter."""

    team_id: TeamId
    channel_id: ChannelId
    root_id: RootId
    actor_id: ActorId
    text: str


@runtime_checkable
class StateStore(Protocol):
    """Protocol implemented by the async persistence adapter.

    This deliberately describes only the cross-adapter primitives. Concrete
    callers can use the richer module-level functions in ``bridge.state``.
    """

    async def mark_event_seen(
        self, team_id: TeamId, event_id: EventId, *, now: int | None = None
    ) -> bool: ...

    async def get_pending_interrogative(
        self, key: ConversationKey, actor_id: ActorId, *, now: int | None = None
    ) -> PendingInterrogative | None: ...


__all__ = [
    "ActorId",
    "ChannelId",
    "ConversationKey",
    "ConversationMode",
    "EventId",
    "Mode",
    "NormalizedMessage",
    "Owner",
    "Participant",
    "ParticipantKind",
    "PendingInterrogative",
    "PromotionBinding",
    "RootId",
    "RootKey",
    "StateStore",
    "TeamId",
    "TextPin",
]

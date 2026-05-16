"""Shared exception types for the cc-bridge daemon."""

from __future__ import annotations


class BotNotReady(RuntimeError):
    """Raised when attempting operations on a bot that hasn't finished connecting."""

    pass

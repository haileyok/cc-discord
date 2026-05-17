"""Mattermost backend for cc-bridge."""

__all__ = ["MattermostBot", "BotNotReady"]

# Import at the end to avoid circular imports
def __getattr__(name: str):
    if name == "MattermostBot":
        from bridge.backends.mattermost.bot import MattermostBot
        return MattermostBot
    elif name == "BotNotReady":
        from bridge.exceptions import BotNotReady
        return BotNotReady
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

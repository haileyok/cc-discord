"""Mattermost backend for cc-bridge."""

__all__ = ["MattermostBot"]

# Import at the end to avoid circular imports
def __getattr__(name: str):
    if name == "MattermostBot":
        from bridge.backends.mattermost.bot import MattermostBot
        return MattermostBot
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

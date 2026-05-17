"""Tests for the Mattermost WebSocket client."""

from __future__ import annotations

import asyncio
import json
from unittest import mock

import pytest

from bridge.backends.mattermost.ws import MattermostWebSocket


class TestMattermostWebSocketURLConstruction:
    """Tests for WebSocket URL construction."""

    def test_build_ws_url_https_to_wss(self):
        """Test https:// URL converts to wss://."""
        url = MattermostWebSocket._build_ws_url("https://mm.example.com")
        assert url == "wss://mm.example.com/api/v4/websocket"

    def test_build_ws_url_http_to_ws(self):
        """Test http:// URL converts to ws://."""
        url = MattermostWebSocket._build_ws_url("http://mm.example.local:8065")
        assert url == "ws://mm.example.local:8065/api/v4/websocket"

    def test_build_ws_url_strips_trailing_slash(self):
        """Test trailing slashes are stripped before building."""
        url = MattermostWebSocket._build_ws_url("https://mm.example.com/")
        assert url == "wss://mm.example.com/api/v4/websocket"


class TestMattermostWebSocketAuthentication:
    """Tests for WebSocket authentication."""

    @pytest.mark.asyncio
    async def test_authentication_message_format(self):
        """Test authentication message has correct format."""
        handler = mock.AsyncMock()
        ws = MattermostWebSocket("https://mm.example.com", "my-token", handler)

        # Mock the websocket connection
        mock_conn = mock.AsyncMock()
        messages_sent = []

        async def capture_send(msg: str) -> None:
            messages_sent.append(json.loads(msg))

        mock_conn.send = capture_send

        await ws._authenticate(mock_conn)

        assert len(messages_sent) == 1
        msg = messages_sent[0]
        assert msg["action"] == "authentication_challenge"
        assert msg["data"]["token"] == "my-token"
        assert msg["seq"] == 1
        assert isinstance(msg["seq"], int)

    @pytest.mark.asyncio
    async def test_authentication_increments_sequence(self):
        """Test sequence number increments."""
        handler = mock.AsyncMock()
        ws = MattermostWebSocket("https://mm.example.com", "token", handler)

        mock_conn = mock.AsyncMock()
        messages_sent = []

        async def capture_send(msg: str) -> None:
            messages_sent.append(json.loads(msg))

        mock_conn.send = capture_send

        await ws._authenticate(mock_conn)
        await ws._authenticate(mock_conn)

        assert messages_sent[0]["seq"] == 1
        assert messages_sent[1]["seq"] == 2


class TestMattermostWebSocketEventDispatching:
    """Tests for event dispatching and decoding."""

    @pytest.mark.asyncio
    async def test_dispatch_posted_event_double_decodes_post(self):
        """Test posted event data.post is double-decoded."""
        handler = mock.AsyncMock()
        ws = MattermostWebSocket("https://mm.example.com", "token", handler)

        post_data = {"id": "post123", "message": "hello", "user_id": "user1"}
        post_json = json.dumps(post_data)

        message = json.dumps(
            {
                "event": "posted",
                "data": {"post": post_json, "mentions": "[]"},
            }
        )

        await ws._dispatch(message)

        handler.assert_called_once()
        event, data = handler.call_args[0]
        assert event == "posted"
        assert data["post"] == post_data
        assert isinstance(data["post"], dict)

    @pytest.mark.asyncio
    async def test_dispatch_posted_event_double_decodes_mentions(self):
        """Test posted event data.mentions is double-decoded."""
        handler = mock.AsyncMock()
        ws = MattermostWebSocket("https://mm.example.com", "token", handler)

        mentions = ["user1", "user2"]
        mentions_json = json.dumps(mentions)

        message = json.dumps(
            {
                "event": "posted",
                "data": {
                    "post": json.dumps({"id": "post123", "message": "hello"}),
                    "mentions": mentions_json,
                },
            }
        )

        await ws._dispatch(message)

        handler.assert_called_once()
        event, data = handler.call_args[0]
        assert data["mentions"] == mentions
        assert isinstance(data["mentions"], list)

    @pytest.mark.asyncio
    async def test_dispatch_reaction_added_event_decodes_reaction(self):
        """Test reaction_added event data.reaction is double-decoded if needed."""
        handler = mock.AsyncMock()
        ws = MattermostWebSocket("https://mm.example.com", "token", handler)

        reaction_data = {
            "user_id": "user123",
            "post_id": "post456",
            "emoji_name": "thumbsup",
        }
        reaction_json = json.dumps(reaction_data)

        message = json.dumps(
            {
                "event": "reaction_added",
                "data": {"reaction": reaction_json},
            }
        )

        await ws._dispatch(message)

        handler.assert_called_once()
        event, data = handler.call_args[0]
        assert event == "reaction_added"
        assert data["reaction"] == reaction_data

    @pytest.mark.asyncio
    async def test_dispatch_reaction_added_event_keeps_dict_reaction(self):
        """Test reaction_added with dict reaction doesn't double-decode."""
        handler = mock.AsyncMock()
        ws = MattermostWebSocket("https://mm.example.com", "token", handler)

        reaction_data = {
            "user_id": "user123",
            "post_id": "post456",
            "emoji_name": "thumbsup",
        }

        message = json.dumps(
            {
                "event": "reaction_added",
                "data": {"reaction": reaction_data},
            }
        )

        await ws._dispatch(message)

        handler.assert_called_once()
        event, data = handler.call_args[0]
        assert event == "reaction_added"
        assert data["reaction"] == reaction_data

    @pytest.mark.asyncio
    async def test_dispatch_ignores_messages_without_event(self):
        """Test messages without event field are ignored."""
        handler = mock.AsyncMock()
        ws = MattermostWebSocket("https://mm.example.com", "token", handler)

        message = json.dumps({"seq": 1})  # No event field

        await ws._dispatch(message)

        handler.assert_not_called()

    @pytest.mark.asyncio
    async def test_dispatch_ignores_invalid_json(self):
        """Test invalid JSON is ignored without crashing."""
        handler = mock.AsyncMock()
        ws = MattermostWebSocket("https://mm.example.com", "token", handler)

        await ws._dispatch("not valid json")

        handler.assert_not_called()

    @pytest.mark.asyncio
    async def test_dispatch_handles_malformed_double_encoded_gracefully(self):
        """Test malformed double-encoded JSON is handled gracefully."""
        handler = mock.AsyncMock()
        ws = MattermostWebSocket("https://mm.example.com", "token", handler)

        message = json.dumps(
            {
                "event": "posted",
                "data": {
                    "post": "not valid json",
                    "mentions": "[]",
                },
            }
        )

        await ws._dispatch(message)

        handler.assert_called_once()
        event, data = handler.call_args[0]
        # Should still call handler, with unparsed string
        assert event == "posted"
        assert data["post"] == "not valid json"


class TestMattermostWebSocketPingHeartbeat:
    """Tests for WebSocket ping/heartbeat configuration."""

    def _make_mock_connect(self, ws_instance: "MattermostWebSocket") -> mock.MagicMock:
        """Create a mock for websockets.connect that allows one connection then stops."""
        mock_conn = mock.AsyncMock()
        mock_conn.__aenter__.return_value = mock_conn
        mock_conn.__aiter__.return_value = mock_conn
        mock_conn.__anext__.side_effect = StopAsyncIteration

        # After __aexit__, set _closing so the while-loop doesn't reconnect
        async def aexit_side_effect(exc_type, exc_val, exc_tb):
            ws_instance._closing = True
            return None

        mock_conn.__aexit__.side_effect = aexit_side_effect

        return mock.MagicMock(return_value=mock_conn)

    @pytest.mark.asyncio
    async def test_connect_called_with_ping_interval(self):
        """Test that websockets.connect is called with ping_interval=30."""
        handler = mock.AsyncMock()
        ws = MattermostWebSocket("https://mm.example.com", "token", handler)
        mock_connect = self._make_mock_connect(ws)

        with mock.patch("bridge.backends.mattermost.ws.websockets.connect", mock_connect):
            await ws._run_loop()

        assert mock_connect.called
        _, kwargs = mock_connect.call_args
        assert kwargs.get("ping_interval") == 30, (
            f"Expected ping_interval=30, got {kwargs.get('ping_interval')!r}"
        )

    @pytest.mark.asyncio
    async def test_connect_called_with_ping_timeout(self):
        """Test that websockets.connect is called with ping_timeout=10."""
        handler = mock.AsyncMock()
        ws = MattermostWebSocket("https://mm.example.com", "token", handler)
        mock_connect = self._make_mock_connect(ws)

        with mock.patch("bridge.backends.mattermost.ws.websockets.connect", mock_connect):
            await ws._run_loop()

        assert mock_connect.called
        _, kwargs = mock_connect.call_args
        assert kwargs.get("ping_timeout") == 10, (
            f"Expected ping_timeout=10, got {kwargs.get('ping_timeout')!r}"
        )


class TestMattermostWebSocketLifecycle:
    """Tests for lifecycle management."""

    @pytest.mark.asyncio
    async def test_closing_flag_set_on_start(self):
        """Test _closing flag is False on start."""
        handler = mock.AsyncMock()
        ws = MattermostWebSocket("https://mm.example.com", "token", handler)

        assert ws._closing is False
        ws._closing = True
        assert ws._closing is True

    @pytest.mark.asyncio
    async def test_close_sets_closing_flag(self):
        """Test close() sets _closing flag."""
        handler = mock.AsyncMock()
        ws = MattermostWebSocket("https://mm.example.com", "token", handler)

        # Mock the websocket
        ws._ws = mock.AsyncMock()
        ws._task = asyncio.create_task(asyncio.sleep(100))

        await ws.close()

        assert ws._closing is True

    @pytest.mark.asyncio
    async def test_start_creates_task(self):
        """Test start() creates an asyncio task."""
        handler = mock.AsyncMock()
        ws = MattermostWebSocket("https://mm.example.com", "token", handler)

        await ws.start()

        assert ws._task is not None
        assert isinstance(ws._task, asyncio.Task)

        # Cleanup
        await ws.close()

    @pytest.mark.asyncio
    async def test_closing_during_startup_prevents_reconnect(self):
        """Test that closing during startup prevents reconnection loop."""
        handler = mock.AsyncMock()
        ws = MattermostWebSocket("https://mm.example.com", "token", handler)

        # Start the run loop
        await ws.start()

        # Immediately close it
        await ws.close()

        # Give it a moment to process
        await asyncio.sleep(0.1)

        # The task should be done (cancelled)
        assert ws._task.done() or ws._task.cancelled()

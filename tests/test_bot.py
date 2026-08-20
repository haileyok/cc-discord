"""Slack adapter acceptance tests.

AC.1 = startup identity/channel validation and health contract.
AC.4 = bounded retry policy and Slack message/file/channel operations.
AC.9 = Socket Mode quick ack and normalized dispatch hooks.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from bridge.bot import (
    MAX_CHUNK,
    Bot,
    BotNotReady,
    SlackAdapterError,
    _chunk,
    _with_retry,
    sanitize_channel_name,
    unique_channel_name,
)
from tests.fakes import (
    FakeEnvelopeAcknowledger,
    FakeSlackClient,
    FakeSocketMode,
    MalformedSlackFixture,
    ScriptedSlackResponse,
    UnexpectedSlackCall,
)


def _startup_script(*, private: bool = True, member: bool = True) -> list[tuple[str, Any]]:
    return [
        ("auth_test", {"ok": True, "team_id": "T1", "user_id": "UBOT"}),
        ("users_info", {"ok": True, "user": {"id": "UOWNER", "name": "owner"}}),
        ("conversations_info", {
            "ok": True,
            "channel": {"id": "GHOME", "name": "home", "team_id": "T1", "is_private": private,
                         "is_member": member},
        }),
    ]


def _ready_bot(*, client: FakeSlackClient | None = None, socket: FakeSocketMode | None = None,
               on_message=None, on_reaction=None) -> Bot:
    return Bot(
        "xoxb-token", team_id="T1", owner_user_id="UOWNER", home_channel_id="GHOME",
        client=client or FakeSlackClient(script=_startup_script()),
        socket_client=socket, on_message=on_message, on_reaction=on_reaction,
    )


class TestTextHelpers:
    def test_chunk_contract(self) -> None:
        assert _chunk("") == [""]
        assert _chunk("a" * MAX_CHUNK) == ["a" * MAX_CHUNK]
        chunks = _chunk("a" * 5000)
        assert all(len(item) <= MAX_CHUNK for item in chunks)
        assert "".join(chunks) == "a" * 5000

    def test_sanitized_unique_names(self) -> None:
        assert sanitize_channel_name("  My résumé / task!!! ") == "my-resume-task"
        assert unique_channel_name("My task", {"my-task"}) == "my-task-2"


class TestAC1StartupAndHealth:
    @pytest.mark.asyncio
    async def test_startup_validates_team_owner_private_home_and_membership(self) -> None:
        client = FakeSlackClient(script=_startup_script())
        socket = FakeSocketMode()
        bot = _ready_bot(client=client, socket=socket)

        await bot.start()

        assert bot.is_ready
        assert bot.health_fields == {
            "bot_connected": True,
            "slack_connected": True,
            "socket_mode_connected": True,
            "team_id": "T1",
            "owner_user_id": "UOWNER",
            "home_channel_id": "GHOME",
            "channel_id": "GHOME",
            "bot_user_id": "UBOT",
            "last_error": None,
        }
        assert [call.method for call in client.calls] == [
            "auth_test", "users_info", "conversations_info"
        ]
        assert socket.connected

    @pytest.mark.asyncio
    async def test_startup_accepts_public_home_channel(self) -> None:
        client = FakeSlackClient(script=_startup_script(private=False, member=True))
        bot = _ready_bot(client=client)
        await bot.start()
        assert bot.is_ready
        assert bot.channel is not None and not bot.channel.is_private

    @pytest.mark.asyncio
    async def test_startup_rejects_nonmember_home(self) -> None:
        client = FakeSlackClient(script=_startup_script(private=False, member=False))
        bot = _ready_bot(client=client)
        with pytest.raises(SlackAdapterError, match="not a member"):
            await bot.start()
        assert not bot.is_ready

    def test_manifest_supports_public_and_private_home_channels(self) -> None:
        manifest = (Path(__file__).parents[1] / "slack-app-manifest.yaml").read_text()
        assert "- message.channels" in manifest
        assert "- message.groups" in manifest
        assert "- channels:history" in manifest
        assert "- groups:history" in manifest

    @pytest.mark.asyncio
    async def test_not_ready_guard(self) -> None:
        bot = Bot("token", home_channel_id="GHOME")
        with pytest.raises(BotNotReady):
            await bot.post("hello")


class TestAC4WebApiAndRetry:
    @pytest.mark.asyncio
    async def test_retries_only_transient_and_honors_retry_after(self) -> None:
        attempts = 0
        waits: list[float] = []

        async def operation() -> str:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise SlackAdapterError("limited", status=429, retry_after=7)
            return "ok"

        assert await _with_retry("test", operation, sleeper=waits.append) == "ok"
        assert attempts == 2
        assert waits == [7]

        async def non_retryable() -> None:
            raise SlackAdapterError("bad auth", status=200, error="invalid_auth")

        with pytest.raises(SlackAdapterError):
            await _with_retry("test", non_retryable, sleeper=waits.append)
        assert waits == [7]

    @pytest.mark.asyncio
    async def test_socket_command_response_posts_ephemeral_to_verified_actor(self) -> None:
        client = FakeSlackClient(script=_startup_script() + [
            ("chat_postEphemeral", {"ok": True, "message_ts": "1.0"}),
        ])
        bot = _ready_bot(client=client)
        await bot.start()
        response = SimpleNamespace(text="missing cwd", blocks=None, ephemeral=True)
        await bot.respond({"channel_id": "GHOME", "actor_id": "UOWNER"}, response)
        call = client.calls[-1]
        assert call.method == "chat_postEphemeral"
        assert call.kwargs == {"channel": "GHOME", "user": "UOWNER", "text": "missing cwd"}

    @pytest.mark.asyncio
    async def test_root_thread_blocks_edit_and_reactions(self) -> None:
        client = FakeSlackClient(script=_startup_script() + [
            ("chat_postMessage", {"ok": True, "ts": "1.1"}),
            ("chat_postMessage", {"ok": True, "ts": "1.2"}),
            ("chat_update", {"ok": True, "ts": "1.2"}),
            ("reactions_add", {"ok": True}),
            ("reactions_add", {"ok": True}),
        ])
        bot = _ready_bot(client=client)
        await bot.start()
        assert await bot.post("root") == ["1.1"]
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "live"}}]
        assert await bot.post_blocks(blocks, "live", root_ts="1.1") == ["1.2"]
        await bot.edit_message("GHOME", "1.2", blocks=blocks, text="fallback")
        await bot.add_reaction("GHOME", "1.2", ":white_check_mark:")
        await bot.add_reaction("GHOME", "1.2", "x")
        calls = client.calls
        post_calls = [call for call in calls if call.method == "chat_postMessage"]
        assert post_calls[1].kwargs["thread_ts"] == "1.1"
        assert post_calls[1].kwargs["text"] == "live"
        assert calls[-1].kwargs["name"] == "x"

    @pytest.mark.asyncio
    async def test_external_upload_flow_is_preflight_upload_complete(self, tmp_path: Path) -> None:
        # The HTTP leg is intentionally not faked by a permissive MagicMock;
        # this verifies the two Slack API legs and file existence/size guards.
        path = tmp_path / "report.txt"
        path.write_text("hello")
        client = FakeSlackClient(script=_startup_script() + [
            ("files_getUploadURLExternal", {
                "ok": True, "upload_url": "https://uploads.slack.test/upload", "file_id": "F1"
            }),
            ("files_completeUploadExternal", {"ok": True, "file_id": "F1", "ts": "2.1"}),
        ])
        bot = _ready_bot(client=client)
        await bot.start()
        # The explicit upload implementation needs an injected aiohttp-like
        # object; a fake that supports async post is deterministic and bounded.
        class Response:
            status = 200
            async def __aenter__(self): return self
            async def __aexit__(self, *args): return None

        class Session:
            def post(self, url, **kwargs):
                assert url.endswith("/upload")
                assert kwargs["allow_redirects"] is False
                assert hasattr(kwargs["data"], "__aiter__")
                return Response()

        bot._http_session = Session()
        result = await bot.upload_file(path, initial_comment="attached")
        assert result.get("file_id") == "F1"
        assert [call.method for call in client.calls[-2:]] == [
            "files_getUploadURLExternal", "files_completeUploadExternal"
        ]

    @pytest.mark.asyncio
    async def test_private_channel_create_invite_and_home_archive_guard(self) -> None:
        client = FakeSlackClient(script=_startup_script() + [
            ("conversations_create", {"ok": True, "channel": {"id": "GNEW"}}),
            ("conversations_info", {"ok": True, "channel": {"id": "GNEW", "team_id": "T1", "is_private": True, "is_member": True}}),
            ("conversations_info", {"ok": True, "channel": {"id": "GNEW", "team_id": "T1", "is_private": True, "is_member": True}}),
            ("conversations_invite", {"ok": True}),
        ])
        bot = _ready_bot(client=client)
        await bot.start()
        assert await bot.create_private_channel("Unsafe Project / 1") == "GNEW"
        assert next(call for call in client.calls if call.method == "conversations_create").kwargs["name"] == "unsafe-project-1"
        with pytest.raises(ValueError, match="home channel"):
            await bot.archive_channel("GHOME")


class TestAC4DownloadAtomicity:
    @pytest.mark.asyncio
    async def test_streaming_download_over_limit_preserves_value_error(self, tmp_path: Path) -> None:
        class Content:
            async def iter_chunked(self, _size):
                yield b"123"
                yield b"456"

        class Response:
            status = 200
            content_length = None
            content = Content()

        class Session:
            def get(self, *args, **kwargs):
                return Response()

        bot = Bot("token", client=FakeSlackClient(), http_session=Session())
        destination = tmp_path / "download.bin"
        with pytest.raises(ValueError, match="exceeds configured size limit") as raised:
            await bot.download_private_file("https://files.slack.com/private", max_bytes=5, destination=destination)
        assert isinstance(raised.value, ValueError)
        assert not destination.exists()
        assert not destination.with_name(destination.name + ".partial").exists()

    def test_atomic_write_preserves_write_error_and_cleans_partial(self, tmp_path: Path, monkeypatch) -> None:
        import bridge.bot as bot_module
        output = tmp_path / "atomic.bin"
        sentinel = OSError("forced write failure")
        original_open = bot_module.os.open

        class FailingHandle:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def write(self, _data): raise sentinel
            def flush(self): pass
            def fileno(self): return 1

        monkeypatch.setattr(bot_module.os, "open", lambda *args, **kwargs: original_open(*args, **kwargs))
        monkeypatch.setattr(bot_module.os, "fdopen", lambda *args, **kwargs: FailingHandle())
        with pytest.raises(OSError) as raised:
            Bot._atomic_write_bytes(output, b"payload")
        assert raised.value is sentinel
        assert not output.exists()
        assert not output.with_name(output.name + ".partial").exists()


class TestAC9SocketMode:
    @pytest.mark.asyncio
    async def test_quick_ack_and_normalized_message_dispatch(self) -> None:
        messages = []
        reactions = []
        socket = FakeSocketMode(FakeEnvelopeAcknowledger(expected=["E1", "E2"]))
        bot = _ready_bot(socket=socket, on_message=messages.append, on_reaction=reactions.append)
        await bot.start()
        await socket.dispatch({
            "envelope_id": "E1",
            "payload": {"team_id": "T1", "event": {"type": "message", "team": "T1", "channel": "GHOME", "user": "U1",
                                     "text": "hello", "ts": "3.1"}},
        })
        await socket.dispatch({
            "envelope_id": "E2",
            "payload": {"team_id": "T1", "event": {"type": "reaction_added", "team": "T1", "user": "U1", "reaction": "eyes",
                                     "item": {"channel": "GHOME", "ts": "3.1"}}},
        })
        assert [message.text for message in messages] == ["hello"]
        assert messages[0].channel_id == "GHOME"
        assert reactions[0]["emoji"] == "eyes"
        socket.acknowledger.assert_complete()

    @pytest.mark.asyncio
    async def test_malformed_socket_fixture_and_strict_call_fail(self) -> None:
        socket = FakeSocketMode()
        bot = _ready_bot(socket=socket)
        await bot.start()
        with pytest.raises(ValueError, match="malformed"):
            await bot.handle_socket_envelope({"envelope_id": "E", "payload": "bad"})
        client = FakeSlackClient(script=[])
        with pytest.raises(UnexpectedSlackCall):
            await client.chat_postMessage(channel="GHOME", text="unexpected")
        with pytest.raises(MalformedSlackFixture):
            await FakeSlackClient(script=[("auth_test", object())]).auth_test()

    def test_interactive_user_object_normalizes_to_stable_actor_id(self) -> None:
        bot = _ready_bot()
        bot._team_id = "T1"
        normalized = bot._normalize_socket_dispatch({
            "envelope_id": "E-action",
            "payload": {
                "type": "block_actions",
                "team": {"id": "T1"},
                "user": {"id": "UOWNER", "username": "owner"},
                "channel": {"id": "GHOME"},
                "message": {"ts": "100.1"},
                "actions": [{"action_id": "task.stats", "value": "task-1"}],
            },
        })
        assert normalized is not None
        assert normalized["actor_id"] == "UOWNER"
        assert normalized["channel_id"] == "GHOME"

    @pytest.mark.asyncio
    async def test_bot_messages_are_not_dispatched(self) -> None:
        received = []
        bot = _ready_bot(on_message=received.append)
        await bot.start()
        await bot.handle_socket_envelope({
            "envelope_id": "E", "payload": {"team_id": "T1", "event": {"type": "message", "team": "T1", "channel": "GHOME",
                                                          "user": "UBOT", "text": "self", "ts": "1"}}
        })
        assert received == []

    @pytest.mark.asyncio
    async def test_bot_self_echo_uses_auth_test_bot_id_and_bot_message_bot_id(self) -> None:
        client = FakeSlackClient(script=[
            ("auth_test", {"ok": True, "team_id": "T1", "user_id": "UBOT", "bot_id": "BSELF"}),
            ("users_info", {"ok": True, "user": {"id": "UOWNER", "name": "owner"}}),
            ("conversations_info", {"ok": True, "channel": {"id": "GHOME", "is_private": True, "is_member": True}}),
        ])
        received = []
        bot = _ready_bot(client=client, on_message=received.append)
        await bot.start()
        assert bot.bot_id == "BSELF"
        await bot.handle_socket_envelope({
            "envelope_id": "E-self",
            "payload": {"team_id": "T1", "event": {"type": "bot_message", "team": "T1", "channel": "GHOME", "bot_id": "BSELF", "username": "bridge", "text": "echo", "ts": "4"}},
        })
        assert received == []

    @pytest.mark.asyncio
    async def test_external_bot_message_uses_b_id_and_invite_kick_use_u_id(self) -> None:
        client = FakeSlackClient(script=_startup_script() + [
            ("bots_info", {"ok": True, "bot": {"id": "BAPP", "user_id": "UAPP"}}),
            ("conversations_info", {"ok": True, "channel": {"id": "GNEW", "team_id": "T1", "is_private": True, "is_member": True}}),
            ("conversations_invite", {"ok": True}),
            ("conversations_info", {"ok": True, "channel": {"id": "GNEW", "team_id": "T1", "is_private": True, "is_member": True}}),
            ("conversations_kick", {"ok": True}),
        ])
        bot = _ready_bot(client=client)
        await bot.start()
        received = []
        bot._on_message_cb = received.append
        await bot.handle_socket_envelope({
            "envelope_id": "E", "payload": {"team_id": "T1", "event": {"type": "bot_message", "team": "T1", "channel": "GNEW",
                                                          "bot_id": "BAPP", "text": "from app", "ts": "2"}}
        })
        assert received and received[0].actor.actor_id == "BAPP"
        assert received[0].actor.is_app
        bot.remember_owned_channel("GNEW")
        await bot.invite_participants("GNEW", ["BAPP"])
        await bot.remove_participants("GNEW", ["BAPP"])
        membership_calls = [call for call in client.calls if call.method in {"conversations_invite", "conversations_kick"}]
        assert [call.kwargs.get("users") or call.kwargs.get("user") for call in membership_calls] == ["UAPP", "UAPP"]
        assert all("BAPP" not in str(call.kwargs) for call in membership_calls)
        client.assert_complete()

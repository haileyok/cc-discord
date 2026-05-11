# cc-bridge Multiplatform — Phase 4: Mattermost Client

**Goal:** Implement `MattermostBot(ChatPlatform)` — a Mattermost backend using raw aiohttp for REST API v4 and the `websockets` library for real-time events.

**Architecture:** Thin async wrapper over Mattermost REST API v4 + WebSocket API. No third-party Mattermost client library — the API surface is small enough that raw aiohttp keeps dependencies minimal and control high. WebSocket client handles `posted` and `reaction_added` events with automatic reconnection. Thread semantics map to Mattermost's `root_id` pattern (root post = thread).

**Tech Stack:** Python 3.12, aiohttp (client), websockets, Mattermost REST API v4

**Scope:** 8 phases from original design (phase 4 of 8)

**Codebase verified:** 2026-05-10

---

## Acceptance Criteria Coverage

This phase implements and tests:

### cc-bridge-multiplatform.AC2: Mattermost Backend Feature Parity (partial)
- **cc-bridge-multiplatform.AC2.1 Success:** Messages posted in a Mattermost channel are relayed to a zellij pane and Claude Code responses stream back to the thread
- **cc-bridge-multiplatform.AC2.6 Success:** Files attached to Mattermost messages are saved locally and paths relayed to Claude
- **cc-bridge-multiplatform.AC2.7 Success:** `[[attach: /path]]` markers in Claude output trigger file upload to Mattermost thread

### cc-bridge-multiplatform.F1: WebSocket Disconnect Recovery
- **cc-bridge-multiplatform.F1:** If Mattermost WebSocket disconnects mid-task, the task continues running and output is posted after reconnection

### cc-bridge-multiplatform.F2: File Upload Failure Graceful Degradation
- **cc-bridge-multiplatform.F2:** If a file upload to Mattermost fails (too large, permission denied), the text portion of the response still posts with an error note

---

<!-- START_TASK_1 -->
### Task 1: Create test infrastructure for Mattermost backend

**Files:**
- Create: `tests/backends/__init__.py` (if not already created in Phase 2)
- Create: `tests/backends/mattermost/__init__.py`
- Create: `tests/backends/mattermost/conftest.py`

**Implementation:**

Set up test directory structure before writing any Mattermost test files:

```bash
mkdir -p tests/backends/mattermost
touch tests/backends/__init__.py
touch tests/backends/mattermost/__init__.py
```

Create shared fixtures in `tests/backends/mattermost/conftest.py`:

```python
import pytest
from tests.fakes import FakePlatform


@pytest.fixture
def fake_platform():
    return FakePlatform()
```

**Verification:**

```bash
uv run pytest tests/backends/mattermost/ -v --collect-only
```

**Commit:** `test: add Mattermost backend test infrastructure`

<!-- END_TASK_1 -->

<!-- START_SUBCOMPONENT_A (tasks 2-3) -->
<!-- START_TASK_2 -->
### Task 2: Add websockets dependency (renamed from original Task 1)

**Files:**
- Modify: `pyproject.toml` (add `websockets` to dependencies)

**Implementation:**

Add `websockets` to the project dependencies in `pyproject.toml`:

```toml
dependencies = [
    # ... existing deps ...
    "websockets>=14",
]
```

**Verification:**

```bash
uv sync
uv run python -c "import websockets; print(websockets.__version__)"
```

**Commit:** `chore: add websockets dependency for Mattermost backend`

<!-- END_TASK_1 -->

<!-- START_TASK_2 -->
### Task 2: Create Mattermost REST API client

**Files:**
- Create: `src/bridge/backends/mattermost/__init__.py`
- Create: `src/bridge/backends/mattermost/api.py`

**Implementation:**

Create the backend directory:

```bash
mkdir -p src/bridge/backends/mattermost
```

Create `__init__.py`:

```python
from bridge.backends.mattermost.bot import MattermostBot

__all__ = ["MattermostBot"]
```

Create `api.py` — a thin async wrapper around Mattermost REST API v4:

```python
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

MAX_MESSAGE_LENGTH = 16383


class MattermostAPI:
    """Thin async wrapper around Mattermost REST API v4."""

    def __init__(self, server_url: str, token: str) -> None:
        self._base = server_url.rstrip("/")
        self._token = token
        self._session: aiohttp.ClientSession | None = None

    @property
    def base_url(self) -> str:
        return self._base

    async def start(self) -> None:
        self._session = aiohttp.ClientSession(
            headers={"Authorization": f"Bearer {self._token}"},
        )

    async def close(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    def _url(self, path: str) -> str:
        return f"{self._base}/api/v4{path}"

    async def _request(
        self, method: str, path: str, **kwargs: Any
    ) -> Any:
        assert self._session is not None, "call start() first"
        async with self._session.request(method, self._url(path), **kwargs) as resp:
            if resp.status == 429:
                retry_after = resp.headers.get("X-RateLimit-Reset", "1")
                raise RateLimitError(float(retry_after))
            resp.raise_for_status()
            if resp.content_type == "application/json":
                return await resp.json()
            return await resp.read()

    async def get_me(self) -> dict:
        return await self._request("GET", "/users/me")

    async def create_post(
        self,
        channel_id: str,
        message: str,
        *,
        root_id: str | None = None,
        file_ids: list[str] | None = None,
    ) -> dict:
        body: dict[str, Any] = {
            "channel_id": channel_id,
            "message": message,
        }
        if root_id:
            body["root_id"] = root_id
        if file_ids:
            body["file_ids"] = file_ids
        return await self._request("POST", "/posts", json=body)

    async def get_post(self, post_id: str) -> dict | None:
        try:
            return await self._request("GET", f"/posts/{post_id}")
        except aiohttp.ClientResponseError as e:
            if e.status == 404:
                return None
            raise

    async def update_post(self, post_id: str, message: str) -> dict:
        return await self._request(
            "PUT", f"/posts/{post_id}", json={"id": post_id, "message": message}
        )

    async def add_reaction(
        self, user_id: str, post_id: str, emoji_name: str
    ) -> dict:
        return await self._request(
            "POST",
            "/reactions",
            json={
                "user_id": user_id,
                "post_id": post_id,
                "emoji_name": emoji_name,
            },
        )

    async def upload_file(
        self, channel_id: str, file_path: Path
    ) -> dict:
        assert self._session is not None
        file_bytes = file_path.read_bytes()
        data = aiohttp.FormData()
        data.add_field("channel_id", channel_id)
        data.add_field(
            "files",
            file_bytes,
            filename=file_path.name,
        )
        return await self._request("POST", "/files", data=data)

    async def download_file(self, file_id: str) -> bytes:
        return await self._request("GET", f"/files/{file_id}")

    async def get_file_info(self, file_id: str) -> dict:
        return await self._request("GET", f"/files/{file_id}/info")


class RateLimitError(Exception):
    def __init__(self, retry_after: float) -> None:
        self.retry_after = retry_after
        super().__init__(f"Rate limited, retry after {retry_after}s")
```

**Testing:**

Tests must verify:
- API client constructs correct URLs
- Bearer token is included in headers
- create_post builds correct request body with root_id and file_ids
- get_post returns None on 404
- upload_file sends multipart form data

Test file: `tests/backends/mattermost/test_api.py`

Use `aiohttp.test_utils` or mock the session for unit tests — follow the project's existing aiohttp testing pattern from `tests/test_server.py`.

**Verification:**

```bash
uv run pytest tests/backends/mattermost/test_api.py -v
```

**Commit:** `feat: add Mattermost REST API client wrapper`

<!-- END_TASK_2 -->
<!-- END_SUBCOMPONENT_A -->

<!-- START_SUBCOMPONENT_B (tasks 3-4) -->
<!-- START_TASK_3 -->
### Task 3: Create Mattermost WebSocket client

**Files:**
- Create: `src/bridge/backends/mattermost/ws.py`

**Implementation:**

Create a WebSocket client that connects to Mattermost, authenticates, and dispatches events. Must handle reconnection with exponential backoff.

Key implementation details from API research:
- Connect to `wss://{server}/api/v4/websocket` (or `ws://` for non-TLS)
- Authenticate immediately after connect with `authentication_challenge` action
- **Critical:** `posted` events have double-encoded JSON in `data.post` — requires two `json.loads()` calls
- `reaction_added` events have `data.reaction` as a dict with `user_id`, `post_id`, `emoji_name`
- Track sequence numbers for request/response correlation

```python
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable, Awaitable

import websockets
from websockets.asyncio.client import ClientConnection

logger = logging.getLogger(__name__)

EventHandler = Callable[[str, dict[str, Any]], Awaitable[None]]


class MattermostWebSocket:
    """Mattermost WebSocket client with auto-reconnection."""

    def __init__(
        self,
        server_url: str,
        token: str,
        event_handler: EventHandler,
    ) -> None:
        self._ws_url = self._build_ws_url(server_url)
        self._token = token
        self._handler = event_handler
        self._seq = 0
        self._ws: ClientConnection | None = None
        self._task: asyncio.Task | None = None
        self._closing = False

    @staticmethod
    def _build_ws_url(server_url: str) -> str:
        url = server_url.rstrip("/")
        if url.startswith("https://"):
            url = "wss://" + url[8:]
        elif url.startswith("http://"):
            url = "ws://" + url[7:]
        return f"{url}/api/v4/websocket"

    async def start(self) -> None:
        self._closing = False
        self._task = asyncio.create_task(self._run_loop())

    async def close(self) -> None:
        self._closing = True
        if self._ws:
            await self._ws.close()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run_loop(self) -> None:
        backoff = 1.0
        max_backoff = 30.0
        while not self._closing:
            try:
                async with websockets.connect(self._ws_url) as ws:
                    self._ws = ws
                    await self._authenticate(ws)
                    backoff = 1.0
                    logger.info("Mattermost WebSocket connected")
                    async for raw in ws:
                        if self._closing:
                            break
                        await self._dispatch(raw)
            except (
                websockets.ConnectionClosed,
                ConnectionError,
                OSError,
            ) as e:
                if self._closing:
                    break
                logger.warning(
                    "WebSocket disconnected: %s — reconnecting in %.1fs",
                    e, backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)
            finally:
                self._ws = None

    async def _authenticate(self, ws: ClientConnection) -> None:
        self._seq += 1
        await ws.send(json.dumps({
            "seq": self._seq,
            "action": "authentication_challenge",
            "data": {"token": self._token},
        }))

    async def _dispatch(self, raw: str | bytes) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Non-JSON WebSocket message: %s", raw[:200])
            return

        event = msg.get("event")
        if not event:
            return

        data = msg.get("data", {})

        # Double-decode posted events — data.post is a JSON string
        if event == "posted" and isinstance(data.get("post"), str):
            try:
                data["post"] = json.loads(data["post"])
            except json.JSONDecodeError:
                pass
        if event == "posted" and isinstance(data.get("mentions"), str):
            try:
                data["mentions"] = json.loads(data["mentions"])
            except json.JSONDecodeError:
                pass

        # reaction_added data.reaction may also be double-encoded
        if event == "reaction_added" and isinstance(data.get("reaction"), str):
            try:
                data["reaction"] = json.loads(data["reaction"])
            except json.JSONDecodeError:
                pass

        await self._handler(event, data)
```

**Testing:**

Tests must verify:
- WebSocket URL construction (https→wss, http→ws)
- Authentication message format
- Double-decode of `posted` event data
- Double-decode of `reaction_added` event data
- Reconnection on disconnect (test with mock websocket)
- `_closing` flag prevents reconnection during shutdown

Test file: `tests/backends/mattermost/test_ws.py`

**Verification:**

```bash
uv run pytest tests/backends/mattermost/test_ws.py -v
```

**Commit:** `feat: add Mattermost WebSocket client with auto-reconnection`

<!-- END_TASK_3 -->

<!-- START_TASK_4 -->
### Task 4: Create MattermostBot implementing ChatPlatform

**Verifies:** cc-bridge-multiplatform.AC2.1, cc-bridge-multiplatform.AC2.6, cc-bridge-multiplatform.AC2.7, cc-bridge-multiplatform.F1, cc-bridge-multiplatform.F2

**Files:**
- Create: `src/bridge/backends/mattermost/bot.py`

**Implementation:**

`MattermostBot` implements `ChatPlatform` by composing `MattermostAPI` and `MattermostWebSocket`. It translates between the protocol's string-based interface and the Mattermost REST API.

Key implementation details:

```python
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Callable, Awaitable

from bridge.backends.mattermost.api import MattermostAPI, RateLimitError, MAX_MESSAGE_LENGTH
from bridge.backends.mattermost.ws import MattermostWebSocket

logger = logging.getLogger(__name__)

CHUNK_LIMIT = 3500  # soft limit, well under the 16383 hard limit


class MattermostBot:
    """Mattermost backend implementing ChatPlatform protocol."""

    def __init__(
        self,
        server_url: str,
        token: str,
        channel_id: str,
        *,
        on_message: Callable[[dict], Awaitable[None]] | None = None,
        on_reaction: Callable[[dict], Awaitable[None]] | None = None,
        allowed_user_ids: list[str] | None = None,
    ) -> None:
        self._api = MattermostAPI(server_url, token)
        self._channel_id = channel_id
        self._on_message = on_message
        self._on_reaction = on_reaction
        self._allowed_user_ids = set(allowed_user_ids) if allowed_user_ids else None
        self._ws: MattermostWebSocket | None = None
        self._ready = False
        self._bot_user_id: str | None = None

    @property
    def is_ready(self) -> bool:
        return self._ready

    async def start(self) -> None:
        await self._api.start()
        me = await self._api.get_me()
        self._bot_user_id = me["id"]
        self._ws = MattermostWebSocket(
            self._api.base_url, self._api._token, self._handle_event
        )
        await self._ws.start()
        self._ready = True

    async def close(self) -> None:
        self._ready = False
        if self._ws:
            await self._ws.close()
        await self._api.close()

    async def post(
        self, message: str, *, thread_id: str | None = None
    ) -> list[str]:
        chunks = _chunk(message, CHUNK_LIMIT)
        msg_ids: list[str] = []
        for chunk in chunks:
            result = await self._api_with_retry(
                lambda c=chunk: self._api.create_post(
                    self._channel_id, c, root_id=thread_id
                )
            )
            msg_ids.append(result["id"])
        return msg_ids

    async def post_with_attachments(
        self,
        file_paths: list[Path],
        *,
        thread_id: str | None = None,
        text: str | None = None,
    ) -> list[str]:
        file_ids: list[str] = []
        failed: list[str] = []
        for fp in file_paths:
            try:
                result = await self._api.upload_file(self._channel_id, fp)
                file_infos = result.get("file_infos", [])
                if file_infos:
                    file_ids.append(file_infos[0]["id"])
            except Exception as e:
                logger.warning("File upload failed for %s: %s", fp.name, e)
                failed.append(fp.name)

        msg = text or ""
        if failed:
            msg += f"\n\n⚠️ Failed to upload: {', '.join(failed)}"

        result = await self._api_with_retry(
            lambda: self._api.create_post(
                self._channel_id,
                msg,
                root_id=thread_id,
                file_ids=file_ids if file_ids else None,
            )
        )
        return [result["id"]]

    async def create_thread(self, name: str) -> str:
        result = await self._api.create_post(
            self._channel_id,
            f"🟢 cc-bridge task: {name}",
        )
        return result["id"]

    async def archive_thread(self, thread_id: str) -> None:
        # Mattermost threads don't archive — no-op
        pass

    async def rename_thread(self, thread_id: str, name: str) -> None:
        # Edit root post content to reflect new name
        await self._api.update_post(
            thread_id, f"🟢 cc-bridge task: {name}"
        )

    async def thread_alive(self, thread_id: str) -> bool:
        result = await self._api.get_post(thread_id)
        return result is not None

    async def download_attachment(
        self, attachment_ref: Any, dest_dir: Path
    ) -> Path:
        # attachment_ref is a dict with "id" and "name" from MM file metadata
        file_id = attachment_ref["id"]
        filename = attachment_ref.get("name", f"{file_id}.bin")
        data = await self._api.download_file(file_id)
        dest = dest_dir / filename
        dest.write_bytes(data)
        return dest

    async def add_reactions(
        self, message_id: str, thread_id: str, emoji: list[str]
    ) -> None:
        assert self._bot_user_id is not None
        for name in emoji:
            mm_name = _emoji_to_mattermost(name)
            await self._api.add_reaction(self._bot_user_id, message_id, mm_name)

    async def edit_message(
        self,
        thread_id: str,
        message_id: str,
        *,
        content: str | None = None,
    ) -> None:
        if content is not None:
            await self._api.update_post(message_id, content)

    async def fetch_messageable(self, thread_id: str) -> Any:
        return thread_id  # MM doesn't have messageable objects

    async def _handle_event(self, event: str, data: dict) -> None:
        if event == "posted":
            post = data.get("post", {})
            # Ignore own posts
            if post.get("user_id") == self._bot_user_id:
                return
            # Check allowed users
            if self._allowed_user_ids and post.get("user_id") not in self._allowed_user_ids:
                return
            # Only handle posts in our channel
            if post.get("channel_id") != self._channel_id:
                return
            if self._on_message:
                await self._on_message(post)

        elif event == "reaction_added":
            reaction = data.get("reaction", {})
            if reaction.get("user_id") == self._bot_user_id:
                return
            if self._on_reaction:
                await self._on_reaction(reaction)

    async def _api_with_retry(
        self, factory: Callable[[], Awaitable[Any]], max_retries: int = 3
    ) -> Any:
        delays = [0.5, 1.5, 4.0]
        for attempt in range(max_retries):
            try:
                return await factory()
            except RateLimitError as e:
                if attempt == max_retries - 1:
                    raise
                await asyncio.sleep(e.retry_after)
            except (aiohttp.ClientError, OSError) as e:
                if attempt == max_retries - 1:
                    raise
                await asyncio.sleep(delays[min(attempt, len(delays) - 1)])


def _chunk(text: str, limit: int = CHUNK_LIMIT) -> list[str]:
    """Split text into chunks, breaking on newlines when possible."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        split_at = text.rfind("\n", 0, limit)
        if split_at == -1:
            split_at = limit
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    return chunks


def _emoji_to_mattermost(emoji: str) -> str:
    """Convert Unicode emoji to Mattermost emoji name."""
    mapping = {
        "✅": "white_check_mark",
        "❌": "x",
        "1️⃣": "one",
        "2️⃣": "two",
        "3️⃣": "three",
        "4️⃣": "four",
        "👍": "thumbsup",
        "👎": "thumbsdown",
    }
    return mapping.get(emoji, emoji)
```

**Testing:**

Tests must verify:
- cc-bridge-multiplatform.AC2.1: post() calls create_post with correct channel_id and root_id
- cc-bridge-multiplatform.AC2.6: download_attachment() saves file to dest_dir with correct name
- cc-bridge-multiplatform.AC2.7: post_with_attachments() uploads files then creates post with file_ids
- cc-bridge-multiplatform.F1: Bot remains functional after WebSocket reconnection (start/close lifecycle)
- cc-bridge-multiplatform.F2: post_with_attachments() still posts text when file upload fails, includes error note
- Message chunking at CHUNK_LIMIT
- Emoji mapping for approval reactions
- Own-post filtering in event handler
- Allowed user filtering

Test file: `tests/backends/mattermost/test_bot.py`

Mock the `MattermostAPI` methods — don't make real HTTP calls.

**Verification:**

```bash
uv run pytest tests/backends/mattermost/test_bot.py -v
```

**Commit:** `feat: implement MattermostBot with ChatPlatform protocol`

<!-- END_TASK_4 -->
<!-- END_SUBCOMPONENT_B -->

<!-- START_TASK_6 -->
### Task 6: Wire voice/audio transcription for Mattermost

**Files:**
- Modify: `src/bridge/backends/mattermost/bot.py` (add audio handling to message processing)

**Implementation:**

The existing codebase has `src/bridge/voice.py` with `transcribe()` for audio attachment handling. The Discord backend splits audio attachments from regular files and transcribes them before relaying to the agent. The Mattermost backend must mirror this behaviour.

When processing a `posted` event with `file_ids`, check each file's MIME type via `GET /api/v4/files/{file_id}/info`. For audio MIME types (`audio/*`), download the file and run it through `voice.transcribe()` — same as the Discord backend does. Successful transcriptions become `[voice memo] <text>` blocks in the relayed prompt; failures fall back to `[voice memo received — transcription unavailable; raw file: <path>]`.

The message handling flow should be:
1. Check for `file_ids` in post data
2. Get file info for each
3. For audio files: download → transcribe → append `[voice memo]` block
4. For non-audio files: download → save to attachments dir → append path
5. Build final message with transcriptions + file paths + original text

**Testing:**

Tests must verify:
- Audio files are detected by MIME type and transcribed
- Non-audio files are saved normally
- Transcription failure falls back gracefully

Test file: `tests/backends/mattermost/test_bot.py` (extend)

**Verification:**

```bash
uv run pytest tests/backends/mattermost/test_bot.py -v
```

**Commit:** `feat: wire voice/audio transcription for Mattermost backend`

<!-- END_TASK_6 -->

**Note on `BRIDGE_MATTERMOST_SCHEME`:** The design plan mentions this env var, but it's unnecessary since `server_url` in secrets.json already includes the scheme (e.g., `https://mm.example.com`). Not implemented — the scheme is part of the URL.

<!-- START_TASK_7 -->
### Task 7: Full regression verification

**Files:**
- None (verification only)

**Verification:**

```bash
uv run pytest -v
```

Expected: All tests pass — existing tests unaffected, new Mattermost tests pass.

<!-- END_TASK_7 -->

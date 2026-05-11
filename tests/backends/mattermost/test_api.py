"""Tests for the Mattermost REST API client."""

import contextlib
import json
from pathlib import Path
from unittest import mock

import aiohttp
import pytest

from bridge.backends.mattermost.api import MattermostAPI, RateLimitError


class TestMattermostAPI:
    """Tests for MattermostAPI client."""

    @pytest.fixture
    def api(self):
        """Return a MattermostAPI instance."""
        return MattermostAPI("https://mm.example.com", "test-token")

    @pytest.mark.asyncio
    async def test_base_url_property(self, api):
        """Test base_url property."""
        assert api.base_url == "https://mm.example.com"

    @pytest.mark.asyncio
    async def test_base_url_strips_trailing_slash(self):
        """Test base_url strips trailing slash."""
        api = MattermostAPI("https://mm.example.com/", "token")
        assert api.base_url == "https://mm.example.com"

    def test_url_construction(self, api):
        """Test _url() constructs correct API path."""
        assert api._url("/posts") == "https://mm.example.com/api/v4/posts"
        assert api._url("/users/me") == "https://mm.example.com/api/v4/users/me"

    @pytest.mark.asyncio
    async def test_start_creates_session(self, api):
        """Test start() creates aiohttp session with auth header."""
        await api.start()
        assert api._session is not None
        # Check Authorization header
        assert api._session.headers.get("Authorization") == "Bearer test-token"
        await api.close()

    @pytest.mark.asyncio
    async def test_close_closes_session(self, api):
        """Test close() closes the session."""
        await api.start()
        await api.close()
        assert api._session is None

    @pytest.mark.asyncio
    async def test_close_idempotent(self, api):
        """Test close() can be called multiple times."""
        await api.start()
        await api.close()
        # Should not raise
        await api.close()

    @pytest.mark.asyncio
    async def test_request_not_started_assertion(self, api):
        """Test _request() asserts session was started."""
        with pytest.raises(AssertionError, match="call start"):
            await api._request("GET", "/users/me")

    @pytest.mark.asyncio
    async def test_get_me(self):
        """Test get_me() calls correct endpoint."""
        api = MattermostAPI("https://mm.example.com", "token")

        with mock.patch.object(api, "_request") as mock_request:
            mock_request.return_value = {"id": "user123", "username": "bot"}

            result = await api.get_me()

            mock_request.assert_called_once_with("GET", "/users/me")
            assert result == {"id": "user123", "username": "bot"}

    @pytest.mark.asyncio
    async def test_create_post_basic(self):
        """Test create_post() with basic message."""
        api = MattermostAPI("https://mm.example.com", "token")

        with mock.patch.object(api, "_request") as mock_request:
            mock_request.return_value = {"id": "post123"}

            result = await api.create_post("channel123", "Hello world")

            mock_request.assert_called_once_with(
                "POST",
                "/posts",
                json={
                    "channel_id": "channel123",
                    "message": "Hello world",
                }
            )
            assert result == {"id": "post123"}

    @pytest.mark.asyncio
    async def test_create_post_with_root_id(self):
        """Test create_post() with thread root_id."""
        api = MattermostAPI("https://mm.example.com", "token")

        with mock.patch.object(api, "_request") as mock_request:
            mock_request.return_value = {"id": "reply123"}

            result = await api.create_post(
                "channel123", "Reply", root_id="root456"
            )

            mock_request.assert_called_once_with(
                "POST",
                "/posts",
                json={
                    "channel_id": "channel123",
                    "message": "Reply",
                    "root_id": "root456",
                }
            )
            assert result == {"id": "reply123"}

    @pytest.mark.asyncio
    async def test_create_post_with_file_ids(self):
        """Test create_post() with file_ids."""
        api = MattermostAPI("https://mm.example.com", "token")

        with mock.patch.object(api, "_request") as mock_request:
            mock_request.return_value = {"id": "post789"}

            result = await api.create_post(
                "channel123",
                "Post with files",
                file_ids=["file1", "file2"]
            )

            mock_request.assert_called_once_with(
                "POST",
                "/posts",
                json={
                    "channel_id": "channel123",
                    "message": "Post with files",
                    "file_ids": ["file1", "file2"],
                }
            )
            assert result == {"id": "post789"}

    @pytest.mark.asyncio
    async def test_create_post_with_all_options(self):
        """Test create_post() with all options."""
        api = MattermostAPI("https://mm.example.com", "token")

        with mock.patch.object(api, "_request") as mock_request:
            mock_request.return_value = {"id": "post999"}

            result = await api.create_post(
                "channel123",
                "Full message",
                root_id="root456",
                file_ids=["file1"]
            )

            mock_request.assert_called_once_with(
                "POST",
                "/posts",
                json={
                    "channel_id": "channel123",
                    "message": "Full message",
                    "root_id": "root456",
                    "file_ids": ["file1"],
                }
            )

    @pytest.mark.asyncio
    async def test_get_post_success(self):
        """Test get_post() returns post data."""
        api = MattermostAPI("https://mm.example.com", "token")

        with mock.patch.object(api, "_request") as mock_request:
            mock_request.return_value = {"id": "post123", "message": "Hello"}

            result = await api.get_post("post123")

            mock_request.assert_called_once_with("GET", "/posts/post123")
            assert result == {"id": "post123", "message": "Hello"}

    @pytest.mark.asyncio
    async def test_get_post_not_found(self):
        """Test get_post() returns None on 404."""
        api = MattermostAPI("https://mm.example.com", "token")

        with mock.patch.object(api, "_request") as mock_request:
            mock_request.side_effect = aiohttp.ClientResponseError(
                request_info=mock.Mock(),
                history=(),
                status=404,
                message="Not Found",
                headers={},
            )

            result = await api.get_post("missing")

            assert result is None

    @pytest.mark.asyncio
    async def test_get_post_other_error(self):
        """Test get_post() re-raises non-404 errors."""
        api = MattermostAPI("https://mm.example.com", "token")

        with mock.patch.object(api, "_request") as mock_request:
            mock_request.side_effect = aiohttp.ClientResponseError(
                request_info=mock.Mock(),
                history=(),
                status=500,
                message="Server Error",
                headers={},
            )

            with pytest.raises(aiohttp.ClientResponseError):
                await api.get_post("post123")

    @pytest.mark.asyncio
    async def test_update_post(self):
        """Test update_post() calls correct endpoint."""
        api = MattermostAPI("https://mm.example.com", "token")

        with mock.patch.object(api, "_request") as mock_request:
            mock_request.return_value = {"id": "post123", "message": "Updated"}

            result = await api.update_post("post123", "Updated message")

            mock_request.assert_called_once_with(
                "PUT",
                "/posts/post123",
                json={"id": "post123", "message": "Updated message"}
            )
            assert result["message"] == "Updated"

    @pytest.mark.asyncio
    async def test_add_reaction(self):
        """Test add_reaction() calls correct endpoint."""
        api = MattermostAPI("https://mm.example.com", "token")

        with mock.patch.object(api, "_request") as mock_request:
            mock_request.return_value = {"emoji_name": "thumbsup"}

            result = await api.add_reaction(
                "user123", "post456", "thumbsup"
            )

            mock_request.assert_called_once_with(
                "POST",
                "/reactions",
                json={
                    "user_id": "user123",
                    "post_id": "post456",
                    "emoji_name": "thumbsup",
                }
            )
            assert result["emoji_name"] == "thumbsup"

    @pytest.mark.asyncio
    async def test_upload_file(self, tmp_path):
        """Test upload_file() sends multipart form data."""
        api = MattermostAPI("https://mm.example.com", "token")

        # Create a test file
        test_file = tmp_path / "test.txt"
        test_file.write_bytes(b"test content")

        # Create a mock response
        mock_resp = mock.Mock()
        mock_resp.status = 200
        mock_resp.content_type = "application/json"

        async def mock_json():
            return {"file_infos": [{"id": "file123", "name": "test.txt"}]}

        mock_resp.json = mock_json

        # Create a context manager for the mock request
        @contextlib.asynccontextmanager
        async def mock_request(*args, **kwargs):
            yield mock_resp

        # Set up session
        mock_session = mock.Mock()
        mock_session.request = mock_request
        api._session = mock_session

        result = await api.upload_file("channel123", test_file)
        assert result["file_infos"][0]["id"] == "file123"

    @pytest.mark.asyncio
    async def test_download_file(self):
        """Test download_file() returns binary data."""
        api = MattermostAPI("https://mm.example.com", "token")

        with mock.patch.object(api, "_request") as mock_request:
            mock_request.return_value = b"binary content"

            result = await api.download_file("file123")

            mock_request.assert_called_once_with("GET", "/files/file123")
            assert result == b"binary content"

    @pytest.mark.asyncio
    async def test_get_file_info(self):
        """Test get_file_info() returns metadata."""
        api = MattermostAPI("https://mm.example.com", "token")

        with mock.patch.object(api, "_request") as mock_request:
            mock_request.return_value = {
                "id": "file123",
                "name": "test.txt",
                "size": 100,
            }

            result = await api.get_file_info("file123")

            mock_request.assert_called_once_with(
                "GET", "/files/file123/info"
            )
            assert result["name"] == "test.txt"

    @pytest.mark.asyncio
    async def test_rate_limit_error(self):
        """Test RateLimitError stores retry_after."""
        error = RateLimitError(2.5)
        assert error.retry_after == 2.5
        assert "Rate limited" in str(error)
        assert "2.5s" in str(error)

    @pytest.mark.asyncio
    async def test_request_handles_rate_limit(self):
        """Test _request() raises RateLimitError on 429."""
        api = MattermostAPI("https://mm.example.com", "token")

        # Create mock response with 429 status
        mock_resp = mock.Mock()
        mock_resp.status = 429
        mock_resp.headers = {"X-RateLimit-Reset": "3.5"}

        # Create a context manager that returns the mock response
        @contextlib.asynccontextmanager
        async def mock_request(*args, **kwargs):
            yield mock_resp

        mock_session = mock.Mock()
        mock_session.request = mock_request
        api._session = mock_session

        with pytest.raises(RateLimitError) as exc_info:
            await api._request("GET", "/test")

        assert exc_info.value.retry_after == 3.5

    @pytest.mark.asyncio
    async def test_request_handles_json_response(self):
        """Test _request() returns JSON when content-type is JSON."""
        api = MattermostAPI("https://mm.example.com", "token")

        mock_resp = mock.Mock()
        mock_resp.status = 200
        mock_resp.content_type = "application/json"

        async def mock_json():
            return {"key": "value"}

        mock_resp.json = mock_json

        @contextlib.asynccontextmanager
        async def mock_request(*args, **kwargs):
            yield mock_resp

        mock_session = mock.Mock()
        mock_session.request = mock_request
        api._session = mock_session

        result = await api._request("GET", "/test")
        assert result == {"key": "value"}

    @pytest.mark.asyncio
    async def test_request_handles_binary_response(self):
        """Test _request() returns binary data for non-JSON."""
        api = MattermostAPI("https://mm.example.com", "token")

        mock_resp = mock.Mock()
        mock_resp.status = 200
        mock_resp.content_type = "image/png"

        async def mock_read():
            return b"binary"

        mock_resp.read = mock_read

        @contextlib.asynccontextmanager
        async def mock_request(*args, **kwargs):
            yield mock_resp

        mock_session = mock.Mock()
        mock_session.request = mock_request
        api._session = mock_session

        result = await api._request("GET", "/test")
        assert result == b"binary"

    @pytest.mark.asyncio
    async def test_request_raise_for_status(self):
        """Test _request() calls raise_for_status()."""
        api = MattermostAPI("https://mm.example.com", "token")

        mock_resp = mock.Mock()
        mock_resp.status = 500
        mock_resp.raise_for_status = mock.Mock(
            side_effect=aiohttp.ClientResponseError(
                request_info=mock.Mock(),
                history=(),
                status=500,
                message="Server Error",
                headers={},
            )
        )

        @contextlib.asynccontextmanager
        async def mock_request(*args, **kwargs):
            yield mock_resp

        mock_session = mock.Mock()
        mock_session.request = mock_request
        api._session = mock_session

        with pytest.raises(aiohttp.ClientResponseError):
            await api._request("GET", "/test")

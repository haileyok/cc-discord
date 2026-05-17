"""Tests for voice.py — audio transcription backend selection and behavior."""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bridge.voice import (
    _AUDIO_SUFFIXES,
    _convert_to_pcm_wav,
    _transcribe_local_whisper,
    _transcribe_wispr,
    is_audio_path,
    transcribe,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_proc(returncode: int = 0, stdout: bytes = b"", stderr: bytes = b"") -> MagicMock:
    """Return a fake asyncio subprocess object."""
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.kill = MagicMock()
    return proc


# ---------------------------------------------------------------------------
# is_audio_path
# ---------------------------------------------------------------------------

class TestIsAudioPath:
    """Tests for the is_audio_path() heuristic."""

    @pytest.mark.parametrize("suffix", list(_AUDIO_SUFFIXES))
    def test_returns_true_for_known_audio_suffixes(self, suffix: str) -> None:
        """All registered audio suffixes are recognized."""
        assert is_audio_path(Path(f"recording{suffix}"))

    def test_returns_false_for_non_audio_suffix(self) -> None:
        """A .txt file is not recognized as audio."""
        assert not is_audio_path(Path("notes.txt"))

    def test_returns_false_for_image_suffix(self) -> None:
        """A .png file is not recognized as audio."""
        assert not is_audio_path(Path("photo.png"))

    def test_is_case_insensitive(self) -> None:
        """Suffix matching should be case-insensitive."""
        assert is_audio_path(Path("recording.MP3"))
        assert is_audio_path(Path("recording.WAV"))

    def test_returns_false_for_no_suffix(self) -> None:
        """A file with no suffix is not recognized as audio."""
        assert not is_audio_path(Path("audiofile"))


# ---------------------------------------------------------------------------
# transcribe() — backend selection
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestTranscribeBackendSelection:
    """transcribe() routes to Wispr when token is set, otherwise local whisper."""

    async def test_uses_wispr_when_token_set(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """When WISPR_FLOW_API_TOKEN is set, _transcribe_wispr is called."""
        monkeypatch.setenv("WISPR_FLOW_API_TOKEN", "tok123")
        audio = tmp_path / "clip.mp3"
        audio.write_bytes(b"fake audio")

        with patch("bridge.voice._transcribe_wispr", new=AsyncMock(return_value="hello")) as mock_wispr:
            result = await transcribe(audio)

        mock_wispr.assert_awaited_once_with(audio, timeout=120.0)
        assert result == "hello"

    async def test_uses_local_whisper_when_no_token(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """When WISPR_FLOW_API_TOKEN is absent, _transcribe_local_whisper is called."""
        monkeypatch.delenv("WISPR_FLOW_API_TOKEN", raising=False)
        audio = tmp_path / "clip.mp3"
        audio.write_bytes(b"fake audio")

        with patch("bridge.voice._transcribe_local_whisper", new=AsyncMock(return_value="world")) as mock_local:
            result = await transcribe(audio)

        mock_local.assert_awaited_once_with(audio, timeout=120.0)
        assert result == "world"

    async def test_passes_custom_timeout_to_backend(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Custom timeout is forwarded to the selected backend."""
        monkeypatch.delenv("WISPR_FLOW_API_TOKEN", raising=False)
        audio = tmp_path / "clip.mp3"
        audio.write_bytes(b"fake audio")

        with patch("bridge.voice._transcribe_local_whisper", new=AsyncMock(return_value=None)) as mock_local:
            await transcribe(audio, timeout=30.0)

        mock_local.assert_awaited_once_with(audio, timeout=30.0)


# ---------------------------------------------------------------------------
# _convert_to_pcm_wav
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestConvertToPcmWav:
    """Tests for the ffmpeg-based audio conversion helper."""

    async def test_returns_wav_path_on_success(self, tmp_path: Path) -> None:
        """Returns a .wispr.wav path alongside the source on ffmpeg success."""
        src = tmp_path / "clip.mp3"
        src.write_bytes(b"fake audio")
        expected = tmp_path / "clip.wispr.wav"

        proc = _make_mock_proc(returncode=0)
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            result = await _convert_to_pcm_wav(src)

        assert result == expected

    async def test_returns_none_when_ffmpeg_missing(self, tmp_path: Path) -> None:
        """Returns None when ffmpeg binary is not found."""
        src = tmp_path / "clip.mp3"
        src.write_bytes(b"fake audio")

        with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError):
            result = await _convert_to_pcm_wav(src)

        assert result is None

    async def test_returns_none_on_ffmpeg_nonzero_exit(self, tmp_path: Path) -> None:
        """Returns None when ffmpeg exits with non-zero return code."""
        src = tmp_path / "clip.mp3"
        src.write_bytes(b"fake audio")

        proc = _make_mock_proc(returncode=1, stderr=b"conversion error")
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            result = await _convert_to_pcm_wav(src)

        assert result is None

    async def test_passes_correct_ffmpeg_args(self, tmp_path: Path) -> None:
        """ffmpeg is invoked with correct 16kHz mono PCM WAV args."""
        src = tmp_path / "clip.mp3"
        src.write_bytes(b"fake audio")
        expected_out = str(tmp_path / "clip.wispr.wav")

        proc = _make_mock_proc(returncode=0)
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)) as mock_exec:
            await _convert_to_pcm_wav(src)

        call_args = mock_exec.call_args[0]
        assert call_args[0] == "ffmpeg"
        assert "-ar" in call_args
        ar_idx = call_args.index("-ar")
        assert call_args[ar_idx + 1] == "16000"
        assert "-ac" in call_args
        ac_idx = call_args.index("-ac")
        assert call_args[ac_idx + 1] == "1"
        assert "-f" in call_args
        f_idx = call_args.index("-f")
        assert call_args[f_idx + 1] == "wav"
        assert expected_out in call_args
        # -y should be present (overwrite output without asking)
        assert "-y" in call_args


# ---------------------------------------------------------------------------
# _transcribe_wispr
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestTranscribeWispr:
    """Tests for the Wispr Flow API transcription path."""

    async def test_returns_none_when_no_token(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Returns None immediately when token is not set."""
        monkeypatch.delenv("WISPR_FLOW_API_TOKEN", raising=False)
        src = tmp_path / "clip.mp3"
        src.write_bytes(b"fake audio")

        result = await _transcribe_wispr(src, timeout=10.0)
        assert result is None

    async def test_returns_none_when_ffmpeg_fails(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Returns None when ffmpeg conversion fails."""
        monkeypatch.setenv("WISPR_FLOW_API_TOKEN", "tok123")
        src = tmp_path / "clip.mp3"
        src.write_bytes(b"fake audio")

        with patch("bridge.voice._convert_to_pcm_wav", new=AsyncMock(return_value=None)):
            result = await _transcribe_wispr(src, timeout=10.0)

        assert result is None

    async def test_sends_base64_encoded_wav_with_bearer_token(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """POSTs base64-encoded WAV bytes with Bearer token auth."""
        monkeypatch.setenv("WISPR_FLOW_API_TOKEN", "tok_abc")
        src = tmp_path / "clip.mp3"
        src.write_bytes(b"fake audio")

        # Create the wav file that _convert_to_pcm_wav would produce
        wav_path = tmp_path / "clip.wispr.wav"
        wav_content = b"\x00\x01\x02\x03audio"
        wav_path.write_bytes(wav_content)

        captured_calls: list[dict] = []

        # Build a fake aiohttp response
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"text": "hello world"})
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        def fake_post(url, headers=None, json=None, timeout=None):
            captured_calls.append({"url": url, "headers": headers, "json": json})
            return mock_resp

        mock_session.post = fake_post

        with patch("bridge.voice._convert_to_pcm_wav", new=AsyncMock(return_value=wav_path)):
            with patch("aiohttp.ClientSession", return_value=mock_session):
                result = await _transcribe_wispr(src, timeout=10.0)

        assert result == "hello world"
        assert len(captured_calls) == 1
        call = captured_calls[0]
        assert "wisprflow.ai" in call["url"]
        assert call["headers"]["Authorization"] == "Bearer tok_abc"
        expected_b64 = base64.b64encode(wav_content).decode("ascii")
        assert call["json"]["audio"] == expected_b64

    async def test_returns_transcription_text_stripped(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Returns text with surrounding whitespace stripped."""
        monkeypatch.setenv("WISPR_FLOW_API_TOKEN", "tok")
        wav_path = tmp_path / "clip.wispr.wav"
        wav_path.write_bytes(b"wav data")

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"text": "  hello  "})
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.post = MagicMock(return_value=mock_resp)

        src = tmp_path / "clip.mp3"
        src.write_bytes(b"fake")
        with patch("bridge.voice._convert_to_pcm_wav", new=AsyncMock(return_value=wav_path)):
            with patch("aiohttp.ClientSession", return_value=mock_session):
                result = await _transcribe_wispr(src, timeout=10.0)

        assert result == "hello"

    async def test_returns_none_on_http_error_status(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Returns None when Wispr API returns 4xx/5xx."""
        monkeypatch.setenv("WISPR_FLOW_API_TOKEN", "tok")
        wav_path = tmp_path / "clip.wispr.wav"
        wav_path.write_bytes(b"wav data")

        for status in (400, 401, 429, 500):
            mock_resp = AsyncMock()
            mock_resp.status = status
            mock_resp.text = AsyncMock(return_value="error detail")
            mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
            mock_resp.__aexit__ = AsyncMock(return_value=False)

            mock_session = MagicMock()
            mock_session.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session.__aexit__ = AsyncMock(return_value=False)
            mock_session.post = MagicMock(return_value=mock_resp)

            src = tmp_path / "clip.mp3"
            src.write_bytes(b"fake")
            with patch("bridge.voice._convert_to_pcm_wav", new=AsyncMock(return_value=wav_path)):
                with patch("aiohttp.ClientSession", return_value=mock_session):
                    result = await _transcribe_wispr(src, timeout=10.0)

            assert result is None, f"expected None for HTTP {status}"

    async def test_returns_none_on_timeout(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Returns None when the HTTP request times out."""
        monkeypatch.setenv("WISPR_FLOW_API_TOKEN", "tok")
        wav_path = tmp_path / "clip.wispr.wav"
        wav_path.write_bytes(b"wav data")

        mock_resp = AsyncMock()
        mock_resp.__aenter__ = AsyncMock(side_effect=asyncio.TimeoutError())
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.post = MagicMock(return_value=mock_resp)

        src = tmp_path / "clip.mp3"
        src.write_bytes(b"fake")
        with patch("bridge.voice._convert_to_pcm_wav", new=AsyncMock(return_value=wav_path)):
            with patch("aiohttp.ClientSession", return_value=mock_session):
                result = await _transcribe_wispr(src, timeout=10.0)

        assert result is None

    async def test_returns_none_on_non_json_response(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Returns None when Wispr returns a non-JSON body."""
        monkeypatch.setenv("WISPR_FLOW_API_TOKEN", "tok")
        wav_path = tmp_path / "clip.wispr.wav"
        wav_path.write_bytes(b"wav data")

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(side_effect=Exception("not json"))
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.post = MagicMock(return_value=mock_resp)

        src = tmp_path / "clip.mp3"
        src.write_bytes(b"fake")
        with patch("bridge.voice._convert_to_pcm_wav", new=AsyncMock(return_value=wav_path)):
            with patch("aiohttp.ClientSession", return_value=mock_session):
                result = await _transcribe_wispr(src, timeout=10.0)

        assert result is None

    async def test_returns_none_when_text_key_missing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Returns None when API response dict has no 'text' key."""
        monkeypatch.setenv("WISPR_FLOW_API_TOKEN", "tok")
        wav_path = tmp_path / "clip.wispr.wav"
        wav_path.write_bytes(b"wav data")

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"result": "ok"})
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.post = MagicMock(return_value=mock_resp)

        src = tmp_path / "clip.mp3"
        src.write_bytes(b"fake")
        with patch("bridge.voice._convert_to_pcm_wav", new=AsyncMock(return_value=wav_path)):
            with patch("aiohttp.ClientSession", return_value=mock_session):
                result = await _transcribe_wispr(src, timeout=10.0)

        assert result is None

    async def test_returns_none_when_text_is_blank(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Returns None when API response text is blank/whitespace-only."""
        monkeypatch.setenv("WISPR_FLOW_API_TOKEN", "tok")
        wav_path = tmp_path / "clip.wispr.wav"
        wav_path.write_bytes(b"wav data")

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"text": "   "})
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.post = MagicMock(return_value=mock_resp)

        src = tmp_path / "clip.mp3"
        src.write_bytes(b"fake")
        with patch("bridge.voice._convert_to_pcm_wav", new=AsyncMock(return_value=wav_path)):
            with patch("aiohttp.ClientSession", return_value=mock_session):
                result = await _transcribe_wispr(src, timeout=10.0)

        assert result is None

    async def test_returns_none_on_client_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Returns None on aiohttp ClientError (network failure)."""
        import aiohttp

        monkeypatch.setenv("WISPR_FLOW_API_TOKEN", "tok")
        wav_path = tmp_path / "clip.wispr.wav"
        wav_path.write_bytes(b"wav data")

        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        # Raise ClientError when post context manager is entered
        mock_resp = AsyncMock()
        mock_resp.__aenter__ = AsyncMock(side_effect=aiohttp.ClientError("connection refused"))
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_session.post = MagicMock(return_value=mock_resp)

        src = tmp_path / "clip.mp3"
        src.write_bytes(b"fake")
        with patch("bridge.voice._convert_to_pcm_wav", new=AsyncMock(return_value=wav_path)):
            with patch("aiohttp.ClientSession", return_value=mock_session):
                result = await _transcribe_wispr(src, timeout=10.0)

        assert result is None


# ---------------------------------------------------------------------------
# _transcribe_local_whisper
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestTranscribeLocalWhisper:
    """Tests for the local whisper CLI transcription path."""

    async def test_returns_transcription_on_success(self, tmp_path: Path) -> None:
        """Returns the text from the .txt output file on success."""
        audio = tmp_path / "clip.mp3"
        audio.write_bytes(b"fake audio")
        out_txt = tmp_path / "clip.txt"

        proc = _make_mock_proc(returncode=0, stderr=b"")

        async def fake_exec(*args, **kwargs):
            # Simulate whisper writing the output file
            out_txt.write_text("this is the transcription")
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            result = await _transcribe_local_whisper(audio, timeout=30.0)

        assert result == "this is the transcription"

    async def test_strips_whitespace_from_output(self, tmp_path: Path) -> None:
        """Returns text stripped of leading/trailing whitespace."""
        audio = tmp_path / "clip.mp3"
        audio.write_bytes(b"fake audio")
        out_txt = tmp_path / "clip.txt"

        proc = _make_mock_proc(returncode=0, stderr=b"")

        async def fake_exec(*args, **kwargs):
            out_txt.write_text("  hello world  \n")
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            result = await _transcribe_local_whisper(audio, timeout=30.0)

        assert result == "hello world"

    async def test_uses_default_binary_and_model(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Uses 'whisper' binary and 'base' model when env vars are unset."""
        monkeypatch.delenv("BRIDGE_WHISPER_BIN", raising=False)
        monkeypatch.delenv("BRIDGE_WHISPER_MODEL", raising=False)
        audio = tmp_path / "clip.mp3"
        audio.write_bytes(b"fake audio")
        out_txt = tmp_path / "clip.txt"

        captured: list[tuple] = []

        async def fake_exec(*args, **kwargs):
            captured.append(args)
            out_txt.write_text("hi")
            return _make_mock_proc(returncode=0)

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            await _transcribe_local_whisper(audio, timeout=30.0)

        assert len(captured) == 1
        argv = captured[0]
        assert argv[0] == "whisper"
        assert "--model" in argv
        model_idx = list(argv).index("--model")
        assert argv[model_idx + 1] == "base"

    async def test_respects_custom_binary_env_var(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Uses BRIDGE_WHISPER_BIN when set."""
        monkeypatch.setenv("BRIDGE_WHISPER_BIN", "/usr/local/bin/faster-whisper")
        audio = tmp_path / "clip.mp3"
        audio.write_bytes(b"fake audio")
        out_txt = tmp_path / "clip.txt"

        captured: list[tuple] = []

        async def fake_exec(*args, **kwargs):
            captured.append(args)
            out_txt.write_text("hi")
            return _make_mock_proc(returncode=0)

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            await _transcribe_local_whisper(audio, timeout=30.0)

        assert captured[0][0] == "/usr/local/bin/faster-whisper"

    async def test_respects_custom_model_env_var(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Uses BRIDGE_WHISPER_MODEL when set."""
        monkeypatch.setenv("BRIDGE_WHISPER_MODEL", "large")
        monkeypatch.delenv("BRIDGE_WHISPER_BIN", raising=False)
        audio = tmp_path / "clip.mp3"
        audio.write_bytes(b"fake audio")
        out_txt = tmp_path / "clip.txt"

        captured: list[tuple] = []

        async def fake_exec(*args, **kwargs):
            captured.append(args)
            out_txt.write_text("hi")
            return _make_mock_proc(returncode=0)

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            await _transcribe_local_whisper(audio, timeout=30.0)

        argv = list(captured[0])
        model_idx = argv.index("--model")
        assert argv[model_idx + 1] == "large"

    async def test_passes_output_format_and_output_dir(self, tmp_path: Path) -> None:
        """Passes --output_format txt and --output_dir to the CLI."""
        audio = tmp_path / "clip.mp3"
        audio.write_bytes(b"fake audio")
        out_txt = tmp_path / "clip.txt"

        captured: list[tuple] = []

        async def fake_exec(*args, **kwargs):
            captured.append(args)
            out_txt.write_text("hi")
            return _make_mock_proc(returncode=0)

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            await _transcribe_local_whisper(audio, timeout=30.0)

        argv = list(captured[0])
        assert "--output_format" in argv
        fmt_idx = argv.index("--output_format")
        assert argv[fmt_idx + 1] == "txt"
        assert "--output_dir" in argv
        dir_idx = argv.index("--output_dir")
        assert argv[dir_idx + 1] == str(tmp_path)

    async def test_audio_path_follows_double_dash(self, tmp_path: Path) -> None:
        """Audio file path appears after '--' sentinel in argv."""
        audio = tmp_path / "clip.mp3"
        audio.write_bytes(b"fake audio")
        out_txt = tmp_path / "clip.txt"

        captured: list[tuple] = []

        async def fake_exec(*args, **kwargs):
            captured.append(args)
            out_txt.write_text("hi")
            return _make_mock_proc(returncode=0)

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            await _transcribe_local_whisper(audio, timeout=30.0)

        argv = list(captured[0])
        assert "--" in argv
        sep_idx = argv.index("--")
        assert argv[sep_idx + 1] == str(audio)

    async def test_returns_none_when_binary_missing(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Returns None when whisper CLI is not installed."""
        monkeypatch.delenv("BRIDGE_WHISPER_BIN", raising=False)
        audio = tmp_path / "clip.mp3"
        audio.write_bytes(b"fake audio")

        with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError):
            result = await _transcribe_local_whisper(audio, timeout=30.0)

        assert result is None

    async def test_returns_none_on_nonzero_exit(self, tmp_path: Path) -> None:
        """Returns None when whisper exits with non-zero return code."""
        audio = tmp_path / "clip.mp3"
        audio.write_bytes(b"fake audio")

        proc = _make_mock_proc(returncode=1, stderr=b"error output")
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            result = await _transcribe_local_whisper(audio, timeout=30.0)

        assert result is None

    async def test_returns_none_on_timeout(self, tmp_path: Path) -> None:
        """Returns None and kills process when whisper times out."""
        audio = tmp_path / "clip.mp3"
        audio.write_bytes(b"fake audio")

        proc = _make_mock_proc(returncode=0)
        proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError())
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            with patch("asyncio.wait_for", side_effect=asyncio.TimeoutError):
                result = await _transcribe_local_whisper(audio, timeout=30.0)

        assert result is None

    async def test_returns_none_when_output_file_not_produced(self, tmp_path: Path) -> None:
        """Returns None when whisper exits 0 but produces no .txt file."""
        audio = tmp_path / "clip.mp3"
        audio.write_bytes(b"fake audio")
        # No .txt file is created

        proc = _make_mock_proc(returncode=0, stderr=b"")
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            result = await _transcribe_local_whisper(audio, timeout=30.0)

        assert result is None

    async def test_returns_none_when_output_file_is_empty(self, tmp_path: Path) -> None:
        """Returns None when the .txt output file is empty."""
        audio = tmp_path / "clip.mp3"
        audio.write_bytes(b"fake audio")
        out_txt = tmp_path / "clip.txt"

        proc = _make_mock_proc(returncode=0, stderr=b"")

        async def fake_exec(*args, **kwargs):
            out_txt.write_text("")
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            result = await _transcribe_local_whisper(audio, timeout=30.0)

        assert result is None

    async def test_clears_stale_txt_before_run(self, tmp_path: Path) -> None:
        """Deletes any existing .txt file before invoking whisper."""
        audio = tmp_path / "clip.mp3"
        audio.write_bytes(b"fake audio")
        stale_txt = tmp_path / "clip.txt"
        stale_txt.write_text("stale content from prior run")

        # Make the proc fail so no new .txt is written
        proc = _make_mock_proc(returncode=1, stderr=b"")
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            result = await _transcribe_local_whisper(audio, timeout=30.0)

        # Stale file should have been removed before whisper ran
        assert not stale_txt.exists()
        assert result is None

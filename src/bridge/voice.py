"""Voice-memo transcription.

Two backends, auto-selected:

  - Wispr Flow API (cloud) when `WISPR_FLOW_API_TOKEN` is set. Exact behavior
    documented at https://api-docs.wisprflow.ai/client_api_transcribe — at the
    time of writing, Wispr only grants tokens to approved orgs, which is why
    this is the opt-in branch rather than the default.
  - Local OpenAI-Whisper CLI otherwise. Install via `pip install -U
    openai-whisper`. Override the binary path with `BRIDGE_WHISPER_BIN` and
    the model with `BRIDGE_WHISPER_MODEL` (default `base`). Compatible
    invocations from `whisper.cpp` / `faster-whisper-cli` should also work
    if their argv accepts `<file> --model <name> --output_format txt
    --output_dir <dir>`; otherwise point `BRIDGE_WHISPER_BIN` at a wrapper
    script.

In both cases ffmpeg is used to normalize the audio first (Wispr requires
16kHz mono PCM WAV; whisper-flavored CLIs are happier with the same).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import os
from pathlib import Path

import aiohttp

from bridge.redaction import safe_error

logger = logging.getLogger(__name__)


WISPR_TRANSCRIBE_URL = "https://api.wisprflow.ai/transcribe"

_AUDIO_SUFFIXES = {
    ".mp3",
    ".m4a",
    ".ogg",
    ".oga",
    ".wav",
    ".webm",
    ".flac",
    ".opus",
    ".aac",
    ".mp4",  # voice memos sometimes ship as .mp4 audio-only
}


def is_audio_path(path: Path) -> bool:
    """Heuristic: does this filename look like audio? Used to route attachments
    to the transcription path."""
    return path.suffix.lower() in _AUDIO_SUFFIXES


async def transcribe(audio_path: Path, *, timeout: float = 120.0) -> str | None:
    """Transcribe an audio file. Returns None if no backend is configured or
    the chosen backend fails. Failures are logged at WARNING."""
    if os.environ.get("WISPR_FLOW_API_TOKEN"):
        return await _transcribe_wispr(audio_path, timeout=timeout)
    return await _transcribe_local_whisper(audio_path, timeout=timeout)


async def _transcribe_wispr(audio_path: Path, *, timeout: float) -> str | None:
    """Wispr Flow REST. Requires base64 16kHz mono PCM WAV (<= 25MB / 6 min)."""
    token = os.environ.get("WISPR_FLOW_API_TOKEN")
    if not token:  # double-check; transcribe() guards but keep this safe
        return None

    wav_path = await _convert_to_pcm_wav(audio_path)
    if wav_path is None:
        return None
    try:
        try:
            if wav_path.stat().st_size > int(os.environ.get("BRIDGE_MAX_AUDIO_BYTES", str(25 * 1024 * 1024))):
                logger.warning("converted audio exceeds configured size limit")
                return None
            audio_bytes = wav_path.read_bytes()
        except OSError:
            logger.warning("failed to read converted audio: %s", safe_error(None, "audio input unavailable"))
            return None
        audio_b64 = base64.b64encode(audio_bytes).decode("ascii")

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        body = {"audio": audio_b64}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    WISPR_TRANSCRIBE_URL,
                    headers=headers,
                    json=body,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as resp:
                    if resp.status >= 400:
                        logger.warning("Wispr transcribe HTTP %d: request rejected", resp.status)
                        return None
                    try:
                        data = await resp.json(content_type=None)
                    except Exception:
                        logger.warning("Wispr transcribe returned a non-JSON response")
                        return None
        except asyncio.TimeoutError:
            logger.warning("Wispr transcribe timed out after %ss", timeout)
            return None
        except aiohttp.ClientError:
            logger.warning("Wispr transcribe HTTP client error")
            return None

        text = data.get("text") if isinstance(data, dict) else None
        if isinstance(text, str) and text.strip():
            return text.strip()
        logger.warning("Wispr transcribe returned no usable text")
        return None
    finally:
        try:
            wav_path.unlink(missing_ok=True)
        except OSError:
            logger.warning("failed to remove temporary Wispr audio")


async def _transcribe_local_whisper(
    audio_path: Path, *, timeout: float
) -> str | None:
    """Shell out to the OpenAI-Whisper CLI (or compatible). Reads the produced
    `<basename>.txt` from the output dir."""
    binary = os.environ.get("BRIDGE_WHISPER_BIN") or "whisper"
    model = os.environ.get("BRIDGE_WHISPER_MODEL") or "base"
    out_dir = audio_path.parent
    expected_out = out_dir / f"{audio_path.stem}.txt"
    # Remove a stale .txt from a prior run so a missing/failed conversion
    # doesn't surface old text.
    try:
        expected_out.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        logger.warning("failed to clear stale transcript output")

    try:
        try:
            # Pass options first, then `--` and the positional audio path.
            proc = await asyncio.create_subprocess_exec(
                binary,
                "--model",
                model,
                "--output_format",
                "txt",
                "--output_dir",
                str(out_dir),
                "--",
                str(audio_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            logger.warning("transcription CLI is unavailable")
            return None

        try:
            await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            with contextlib.suppress(asyncio.CancelledError, ProcessLookupError):
                await proc.wait()
            logger.warning("transcribe timed out after %ss", timeout)
            return None
        except BaseException:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(BaseException):
                await proc.wait()
            raise
        if proc.returncode != 0:
            logger.warning("transcription CLI failed")
            return None
        if not expected_out.is_file():
            logger.warning("transcribe produced no output")
            return None
        try:
            text = expected_out.read_text(errors="replace").strip()
        except OSError:
            logger.warning("failed to read transcript output")
            return None
        return text or None
    finally:
        with contextlib.suppress(OSError):
            expected_out.unlink()


async def _convert_to_pcm_wav(src: Path) -> Path | None:
    """Run ffmpeg to produce a 16kHz mono PCM WAV alongside `src`. Returns the
    wav path or None on failure (binary missing, conversion error)."""
    out = src.with_name(src.stem + ".wispr.wav")
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-y",
            "-i",
            str(src),
            "-ar",
            "16000",
            "-ac",
            "1",
            "-f",
            "wav",
            str(out),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        logger.warning("ffmpeg is unavailable")
        with contextlib.suppress(OSError):
            out.unlink()
        return None
    try:
        await proc.communicate()
    except BaseException:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(asyncio.CancelledError, ProcessLookupError):
            await proc.wait()
        with contextlib.suppress(OSError):
            out.unlink()
        raise
    if proc.returncode != 0:
        logger.warning("ffmpeg conversion failed")
        with contextlib.suppress(OSError):
            out.unlink()
        return None
    return out

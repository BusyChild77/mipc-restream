"""Tests for minting a URL and handing it to ffmpeg."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from mipc_client import MipcConnectionError
from mipc_restream import stream
from mipc_restream.config import Settings

from .conftest import SERIAL

URL = "rtsp://relay.example:8554/live/MIPC0000001_p0_TOKEN"
OUTPUT = "rtsp://127.0.0.1:8554/front_door"


class FakeStderr:
    """Stands in for the pipe ffmpeg writes its diagnostics to."""

    def __init__(self, lines: list[bytes]) -> None:
        """Keep the lines to hand out, one per iteration."""
        self._lines = list(lines)

    def __aiter__(self) -> FakeStderr:
        """Iterating the pipe is how the relay reads it."""
        return self

    async def __anext__(self) -> bytes:
        """Return the next line, ending when ffmpeg would have closed the pipe."""
        if not self._lines:
            raise StopAsyncIteration

        return self._lines.pop(0)


class FakeProcess:
    """Stands in for the ffmpeg process."""

    def __init__(self, lines: list[bytes] | None = None, code: int = 0) -> None:
        """Set up what ffmpeg prints and what it exits with."""
        self.stderr = FakeStderr(lines or [])
        self.terminated = False
        self._code = code

    async def wait(self) -> int:
        """Return the exit code."""
        return self._code

    def terminate(self) -> None:
        """Record that a signal was passed on."""
        self.terminated = True


@pytest.fixture
def mint() -> Any:
    """Make the MIPC client answer with a stream URL, without any network."""
    client = AsyncMock()
    client.async_get_stream_url.return_value = URL

    with patch.object(stream, "create_client", return_value=client):
        yield client


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (
            b"rtsp://relay/live/SECRET: 404 Not Found\n",
            b"rtsp://<redacted>: 404 Not Found\n",
        ),
        (b"Opening 'https://host/a?token=abc'\n", b"Opening 'https://<redacted>'\n"),
        (
            b"Non-monotonic DTS in output stream\n",
            b"Non-monotonic DTS in output stream\n",
        ),
    ],
)
def test_urls_never_survive_into_the_log(line: bytes, expected: bytes) -> None:
    """The stream URL is a bearer token, and ffmpeg quotes it back at us."""
    assert stream.redact(line) == expected


def test_the_command_copies_over_tcp(settings: Settings) -> None:
    """MIPC's relay drops UDP often enough that TCP is not optional."""
    command = stream.command(URL, OUTPUT, settings)

    assert command[0] == "ffmpeg"
    assert command[command.index("-timeout") + 1] == str(30_000_000)
    assert command[command.index("-i") + 1] == URL
    assert command[-1] == OUTPUT
    assert command[command.index("-c:v") + 1] == "copy"
    assert command.count("-rtsp_transport") == 2


def test_the_command_starts_without_waiting_out_ffmpeg_s_defaults(
    settings: Settings,
) -> None:
    """The probe limits are what make the first connection succeed.

    ffmpeg's defaults spend thirteen seconds deciding what a MIPC stream holds,
    and go2rtc makes the consumer wait for all of it, so VLC gives up and the
    camera looks dead. These belong before ``-i``: they describe the input.
    """
    command = stream.command(URL, OUTPUT, settings)

    for option in ("-analyzeduration", "-probesize", "-fflags"):
        assert command.index(option) < command.index("-i")

    assert command[command.index("-analyzeduration") + 1] == str(1_000_000)
    assert command[command.index("-probesize") + 1] == str(500_000)
    assert command[command.index("-fflags") + 1] == "nobuffer"


def test_the_command_takes_the_configured_ffmpeg_arguments() -> None:
    """Someone who really does want to transcode is allowed to say so."""
    settings = Settings(username="u", password="p", ffmpeg_args=("-c:v", "h264"))

    assert "h264" in stream.command(URL, OUTPUT, settings)


def test_the_camera_s_audio_is_never_negotiated_by_default(
    settings: Settings,
) -> None:
    """Waiting for MIPC's AAC track is what tied startup to the read timeout.

    Refusing it at the RTSP layer is what lets the timeout be a patient
    watchdog instead of a five second fuse, so this belongs before ``-i``.
    """
    command = stream.command(URL, OUTPUT, settings)

    assert command.index("-allowed_media_types") < command.index("-i")
    assert command[command.index("-allowed_media_types") + 1] == "video"


def test_a_silent_track_stands_in_for_the_audio_that_was_refused(
    settings: Settings,
) -> None:
    """An NVR recording sound emits `-map 0:a` and dies without a track.

    The silence is a second input, so the mapping has to be explicit: video
    from MIPC, audio from lavfi. `-re` paces it, or it runs away from the
    video immediately.
    """
    command = stream.command(URL, OUTPUT, settings)

    assert command[command.index(stream._SILENCE) - 1] == "-i"
    assert command.index("-re") < command.index(stream._SILENCE)
    assert command.index(URL) < command.index(stream._SILENCE)
    assert command[command.index("-map") + 1] == "0:v"
    assert command[command.index("-map", command.index("-map") + 1) + 1] == "1:a"


def test_the_camera_s_own_audio_can_be_asked_for() -> None:
    """Whoever wants the real sound pays the slow start for it."""
    settings = Settings(username="u", password="p", audio="camera")
    command = stream.command(URL, OUTPUT, settings)

    assert "-allowed_media_types" not in command
    assert stream._SILENCE not in command
    assert command[-7:-5] == ["-c", "copy"]


def test_the_audio_can_be_dropped_altogether() -> None:
    """No track at all, for whoever knows nothing downstream asks for one."""
    settings = Settings(username="u", password="p", audio="none")
    command = stream.command(URL, OUTPUT, settings)

    assert command[command.index("-allowed_media_types") + 1] == "video"
    assert stream._SILENCE not in command


async def test_the_session_is_released_once_the_url_is_minted(
    mint: Any, settings: Settings
) -> None:
    """The URL outlives the session, so nothing is gained by holding it open."""
    assert await stream.async_resolve(SERIAL, settings) == URL

    mint.async_get_stream_url.assert_awaited_once_with(SERIAL, profile="p0")
    mint.async_close.assert_awaited_once()


async def test_the_session_is_released_even_when_mipc_refuses(
    mint: Any, settings: Settings
) -> None:
    """A failure must not leak the connection it failed on."""
    mint.async_get_stream_url.side_effect = MipcConnectionError("down")

    with pytest.raises(MipcConnectionError):
        await stream.async_resolve(SERIAL, settings)

    mint.async_close.assert_awaited_once()


async def test_running_a_stream_relays_ffmpeg_and_its_exit_code(
    mint: Any, settings: Settings, capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    """go2rtc watches the exit code, and a human watches the diagnostics."""
    process = FakeProcess([b"rtsp://relay/live/SECRET: timeout\n"], code=8)
    spawn = AsyncMock(return_value=process)

    with patch.object(stream, "create_subprocess_exec", spawn):
        assert await stream.async_run(SERIAL, OUTPUT, settings) == 8

    assert spawn.await_args.args[0] == "ffmpeg"
    assert URL in spawn.await_args.args
    assert capsysbinary.readouterr().err == b"rtsp://<redacted>: timeout\n"


async def test_a_signal_is_passed_on_to_ffmpeg(mint: Any, settings: Settings) -> None:
    """Killing this process must close the connection MIPC thinks is watching."""
    process = FakeProcess()

    with patch.object(
        stream, "create_subprocess_exec", AsyncMock(return_value=process)
    ):
        await stream.async_run(SERIAL, OUTPUT, settings)

    stream._terminate(process)

    assert process.terminated


def test_terminating_a_process_that_already_died_is_harmless() -> None:
    """ffmpeg exiting a moment before the signal arrives is not an error."""
    process = FakeProcess()
    process.terminate = _raise_process_lookup  # type: ignore[method-assign]

    stream._terminate(process)


def _raise_process_lookup() -> None:
    """Behave like a process that is already gone."""
    raise ProcessLookupError

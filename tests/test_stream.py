"""Tests for minting a URL and handing it to ffmpeg."""

from __future__ import annotations

from asyncio import Event, Future, get_running_loop, sleep
from dataclasses import replace
from signal import SIGKILL
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest

from mipc_client import (
    MipcAuthenticationError,
    MipcConnectionError,
    MipcSessionExpiredError,
)
from mipc_restream import stream
from mipc_restream.config import OFFLINE_CODE, Settings

from .conftest import SERIAL

URL = "rtsp://relay.example:8554/live/MIPC0000001_p0_TOKEN"
OUTPUT = "rtsp://127.0.0.1:8554/front_door"


class FakePipe:
    """Stands in for one of the pipes ffmpeg writes down.

    A pipe that has run out of lines but whose process is still alive does not
    return: ffmpeg keeps its end open for as long as it lives, and a watchdog
    that only ever saw a closed pipe would never have anything to watch.
    """

    def __init__(self, lines: list[bytes], eof: bool = True) -> None:
        """Keep the lines to hand out, and say whether the pipe then closes."""
        self._lines = list(lines)
        self._eof = eof
        self._closed = Event()

    def __aiter__(self) -> FakePipe:
        """Iterating the pipe is how the relay reads it."""
        return self

    async def __anext__(self) -> bytes:
        """Return the next line, ending when ffmpeg would have closed the pipe."""
        line = await self.readline()
        if not line:
            raise StopAsyncIteration

        return line

    async def readline(self) -> bytes:
        """Return the next line, waiting if ffmpeg is alive and saying nothing."""
        if self._lines:
            return self._lines.pop(0)

        if not self._eof:
            await self._closed.wait()

        return b""

    def close(self) -> None:
        """Let go of anything waiting, the way an exiting process does."""
        self._eof = True
        self._closed.set()


class FakeProcess:
    """Stands in for the ffmpeg process."""

    def __init__(
        self,
        lines: list[bytes] | None = None,
        code: int = 0,
        progress: list[bytes] | None = None,
        alive: bool = False,
        deaf: bool = False,
    ) -> None:
        """Set up what ffmpeg prints, what it exits with, and how it dies.

        ``alive`` keeps both pipes open after the lines run out, which is what
        a stall looks like. ``deaf`` ignores the polite signal.
        """
        self.stderr = FakePipe(lines or [], eof=not alive)
        self.stdout = FakePipe(progress or [], eof=not alive)
        self.terminated = False
        self.killed = False
        self._code = code
        self._deaf = deaf

    async def wait(self) -> int:
        """Return the exit code, or hang like an ffmpeg that ignores signals."""
        if self._deaf and not self.killed:
            await Future()

        return self._code

    def terminate(self) -> None:
        """Record that a signal was passed on, and exit unless deaf to it."""
        self.terminated = True
        if not self._deaf:
            self._exit()

    def kill(self) -> None:
        """Record that the polite signal was not enough."""
        self.killed = True
        self._exit()

    def _exit(self) -> None:
        """Close both pipes, the way a process that has gone does."""
        self.stderr.close()
        self.stdout.close()


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


def test_the_muxer_is_not_left_sitting_on_a_packet(settings: Settings) -> None:
    """A second, not the ten ffmpeg holds a sparse stream back by default.

    This was added believing the silence would keep the connection warm through
    a video stall. Measured against an RTSP sink, it does not: when the video
    stops, every counter freezes within a second and nothing reaches the sink
    at all, silence included. It stays as a bound on muxer latency, and because
    it is the sort of thing an override wants to reach. This belongs after the
    inputs: it describes the output.
    """
    command = stream.command(URL, OUTPUT, settings)

    assert command.index("-max_interleave_delta") > command.index(stream._SILENCE)
    assert command[command.index("-max_interleave_delta") + 1] == str(1_000_000)


def test_the_interleave_delta_is_left_to_an_override(settings: Settings) -> None:
    """Whoever really means it gets the last word, as they do everywhere here.

    ffmpeg takes the last occurrence of an option, so the default has to come
    first for `MIPC_FFMPEG_ARGS` to be able to replace it.
    """
    settings = replace(settings, ffmpeg_args=("-max_interleave_delta", "0"))
    command = stream.command(URL, OUTPUT, settings)

    ours = command.index("-max_interleave_delta")
    theirs = command.index("-max_interleave_delta", ours + 1)

    assert command[ours + 1] == str(1_000_000)
    assert command[theirs + 1] == "0"


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
    """A failure must not leak the connection it failed on, on any attempt."""
    mint.async_get_stream_url.side_effect = MipcConnectionError("down")

    with pytest.raises(MipcConnectionError):
        await stream.async_resolve(SERIAL, settings)

    assert mint.async_close.await_count == mint.async_get_stream_url.await_count


async def test_a_blip_is_ridden_out_inside_the_connection(
    mint: Any, settings: Settings
) -> None:
    """A relay having a bad moment should never reach the recorder as a drop."""
    mint.async_get_stream_url.side_effect = [MipcConnectionError("down"), URL]

    assert await stream.async_resolve(SERIAL, settings) == URL
    assert mint.async_get_stream_url.await_count == 2


async def test_a_camera_that_is_not_there_is_asked_once(
    mint: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """A camera off MIPC's cloud comes back when someone sees to it, not on a retry.

    Asking again inside one connection cannot help, and every ask spends one of
    the few sessions the camera has for when it does return.
    """
    caplog.set_level("INFO")
    settings = Settings(username=SERIAL, password="p")
    mint.async_get_stream_url.side_effect = MipcSessionExpiredError(OFFLINE_CODE)

    with pytest.raises(MipcSessionExpiredError):
        await stream.async_resolve(SERIAL, settings)

    assert mint.async_get_stream_url.await_count == 1
    assert "Holding" in caplog.text


async def test_a_displaced_session_is_asked_again(
    mint: Any, settings: Settings
) -> None:
    """The identical code on an email account means signing in again is the fix."""
    mint.async_get_stream_url.side_effect = [
        MipcSessionExpiredError(OFFLINE_CODE),
        URL,
    ]

    assert await stream.async_resolve(SERIAL, settings) == URL


async def test_credentials_mipc_has_rejected_are_not_replayed(
    mint: Any, settings: Settings
) -> None:
    """Replaying a rejected password could lock the account; nothing else here can."""
    mint.async_get_stream_url.side_effect = MipcAuthenticationError(
        "accounts.pass.invalid"
    )

    with pytest.raises(MipcAuthenticationError):
        await stream.async_resolve(SERIAL, settings)

    assert mint.async_get_stream_url.await_count == 1


async def test_a_failure_is_never_quick(mint: Any, settings: Settings) -> None:
    """Failing at once is what turns one absent camera into a login every second.

    go2rtc does not pace an `exec:` source it has no retry for, so the cadence
    is whatever reconnects — and a recorder reconnects immediately. Holding the
    budget is the only brake on the path.
    """
    mint.async_get_stream_url.side_effect = MipcAuthenticationError(
        "accounts.pass.invalid"
    )
    loop = get_running_loop()

    started = loop.time()
    with pytest.raises(MipcAuthenticationError):
        await stream.async_resolve(SERIAL, settings)

    assert loop.time() - started >= stream._RESOLVE_BUDGET


async def test_a_mipc_that_answers_too_slowly_ends_here_not_at_go2rtc(
    mint: Any, settings: Settings
) -> None:
    """The command has thirty seconds to announce, however long MIPC takes."""

    async def never(*_: object, **__: object) -> str:
        await sleep(stream._RESOLVE_BUDGET * 10)

        return URL

    mint.async_get_stream_url.side_effect = never
    loop = get_running_loop()

    started = loop.time()
    with pytest.raises(MipcConnectionError):
        await stream.async_resolve(SERIAL, settings)

    assert loop.time() - started < stream._RESOLVE_BUDGET * 1.5


async def test_the_hold_stays_inside_what_go2rtc_allows(
    mint: Any, settings: Settings
) -> None:
    """go2rtc kills a command that has not announced in thirty seconds."""
    mint.async_get_stream_url.side_effect = MipcConnectionError("down")
    loop = get_running_loop()

    started = loop.time()
    with pytest.raises(MipcConnectionError):
        await stream.async_resolve(SERIAL, settings)

    assert loop.time() - started < stream._RESOLVE_BUDGET * 1.5


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


def test_ffmpeg_is_made_to_say_whether_anything_is_still_moving(
    settings: Settings,
) -> None:
    """Nothing else ffmpeg prints tells a stalled stream from a working one.

    A global option, so it sits with the other global ones and leaves the
    output arguments — which `MIPC_FFMPEG_ARGS` replaces whole — alone.
    """
    command = stream.command(URL, OUTPUT, settings)

    assert command[command.index("-progress") + 1] == "pipe:1"
    assert command.index("-progress") < command.index("-i")


async def test_a_stream_that_has_stopped_delivering_is_ended(
    mint: Any, settings: Settings
) -> None:
    """The failure nothing else here could see, and the reason for all of it.

    ffmpeg's read timeout is satisfied by a relay that answers keepalives, and
    go2rtc's fifteen seconds end in a SIGKILL that orphans the ffmpeg holding
    the MIPC session. A hundredth of a second stands in for the twelve, so the
    test does not sit through them.
    """
    process = FakeProcess(progress=[b"out_time_us=1000000\n"], alive=True)

    with patch.object(
        stream, "create_subprocess_exec", AsyncMock(return_value=process)
    ):
        await stream.async_run(SERIAL, OUTPUT, replace(settings, stall_timeout=0.01))

    assert process.terminated
    assert not process.killed


async def test_a_counter_that_only_follows_the_clock_is_not_delivery(
    mint: Any, settings: Settings
) -> None:
    """What a stalled MIPC stream actually reports, twice a second, forever.

    ffmpeg keeps talking; the only thing that changes is how far behind real
    time it has fallen. Reading `speed` as progress would call that healthy.
    """
    process = FakeProcess(
        progress=[
            b"out_time_us=5900000\n",
            b"speed=0.98x\n",
            b"out_time_us=5900000\n",
            b"speed=0.51x\n",
        ],
        alive=True,
    )

    with patch.object(
        stream, "create_subprocess_exec", AsyncMock(return_value=process)
    ):
        await stream.async_run(SERIAL, OUTPUT, replace(settings, stall_timeout=0.01))

    assert process.terminated


async def test_a_stream_that_is_delivering_is_left_alone(
    mint: Any, settings: Settings
) -> None:
    """The watchdog ends when ffmpeg does, and not for any other reason."""
    process = FakeProcess(
        progress=[
            b"frame=1\n",
            b"out_time_us=1000000\n",
            b"progress=continue\n",
            b"frame=26\n",
            b"out_time_us=2000000\n",
        ]
    )

    with patch.object(
        stream, "create_subprocess_exec", AsyncMock(return_value=process)
    ):
        await stream.async_run(SERIAL, OUTPUT, settings)

    assert not process.terminated


async def test_an_ffmpeg_that_ignores_the_signal_is_killed(
    mint: Any, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Politeness is for the TEARDOWN, not for the camera's viewer slot."""
    monkeypatch.setattr(stream, "_GRACE", 0.01)
    process = FakeProcess(progress=[b"out_time_us=1000000\n"], alive=True, deaf=True)

    with patch.object(
        stream, "create_subprocess_exec", AsyncMock(return_value=process)
    ):
        await stream.async_run(SERIAL, OUTPUT, replace(settings, stall_timeout=0.01))

    assert process.terminated
    assert process.killed


async def test_ffmpeg_is_told_to_die_with_this_process(
    mint: Any, settings: Settings
) -> None:
    """go2rtc's SIGKILL cannot be caught, so the kernel is asked in advance."""
    spawn = AsyncMock(return_value=FakeProcess())

    with patch.object(stream, "create_subprocess_exec", spawn):
        await stream.async_run(SERIAL, OUTPUT, settings)

    assert spawn.await_args.kwargs["preexec_fn"] is stream._die_with_parent


def test_the_kernel_is_asked_to_end_ffmpeg_with_its_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An orphaned ffmpeg is a MIPC session nobody will ever close.

    A SIGKILL and not a SIGTERM: this is reached only once something has
    killed this process outright, and an ffmpeg wedged enough to get here is
    the one that would ignore the polite signal. `_async_stop` is where a
    TEARDOWN is asked for nicely.
    """
    libc = Mock()
    monkeypatch.setattr(stream, "_LIBC", libc)

    stream._die_with_parent()

    libc.prctl.assert_called_once_with(stream._PR_SET_PDEATHSIG, SIGKILL)


def test_a_kernel_that_has_no_pdeathsig_is_not_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The container is always Linux; whoever is running the tests may not be."""
    monkeypatch.setattr(stream, "_LIBC", Mock(spec=[]))

    stream._die_with_parent()

"""Mint one stream URL and hand it to ffmpeg, for the life of one connection.

This is what go2rtc runs per consumer. It exists as a wrapper around ffmpeg
rather than as a plain ffmpeg command line for three reasons: the URL is only
knowable a moment before it is used; ffmpeg prints whatever URL it was given
into its own error messages, which would put a bearer token in the logs; and
ffmpeg cannot tell that a stream it is still connected to has stopped arriving,
so something has to watch it and end it when it has.
"""

from __future__ import annotations

import logging
import re
import sys
from asyncio import (
    CancelledError,
    StreamReader,
    create_subprocess_exec,
    gather,
    get_running_loop,
    wait_for,
)
from asyncio.subprocess import Process
from contextlib import suppress
from ctypes import CDLL
from signal import SIGINT, SIGKILL, SIGTERM
from subprocess import DEVNULL, PIPE
from typing import Final

from mipc_client import MipcClient, create_client

from .config import Settings

__all__ = ["async_run", "command", "redact"]

LOGGER = logging.getLogger("mipc_restream")

#: Any URL is treated as a secret. The MIPC one carries a single use token, and
#: nothing else ffmpeg prints a URL for is worth leaking either. The last
#: character may not be punctuation, so that the ``:`` in ffmpeg's usual
#: ``<url>: Connection refused`` survives and the message still reads.
_URL: Final = re.compile(rb"\b([a-z][a-z0-9+.-]*)://[^\s'\"]*[^\s'\".,:;!?]")

#: Read timeout, in microseconds, so an upstream that has closed its socket ends
#: the process instead of holding a monitor open on a stream that stopped
#: arriving.
#:
#: This used to double as the startup cost: time to first output tracked the
#: option almost exactly — 15s gave 15.5s, 5s gave 5.4s, omitting it gave 17.3s
#: — because ffmpeg sat in its stream probe waiting for MIPC's AAC track and
#: only the socket timeout ever ended the wait. That forced the timeout down to
#: five seconds, which is far too short a fuse for a path across the internet:
#: an ordinary relay stall killed the stream, and go2rtc had to mint a new
#: session while the recorder showed black.
#:
#: Refusing the audio track at the RTSP layer is what separated them, so the
#: timeout can afford to be patient. It is not the stall watchdog, whatever its
#: name suggests: it measures silence on the *socket*, and MIPC's relay answers
#: ffmpeg's RTSP keepalives whether or not the camera behind it is still sending
#: video. A relay that keeps talking while the camera has stopped never trips
#: it. That is what ``_async_watch`` is for.
_MICROSECONDS: Final = 1_000_000

#: A track of silence, so an output always has audio to offer even when the
#: camera's own is never asked for. Mono at 16 kHz because nothing listens to
#: it; it exists so a recorder's ``-map 0:a`` finds something.
_SILENCE: Final = "anullsrc=channel_layout=mono:sample_rate=16000"

#: How long the muxer may hold a packet back to interleave it, in microseconds.
#:
#: This was added believing the silence would keep the connection warm through a
#: stall, and it does not: measured against an RTSP sink, a video input that
#: stops while its socket stays open freezes ``out_time_us`` and every other
#: counter within a second, silence included, and nothing at all reaches the
#: sink for as long as the stall lasts. The muxer will not run ahead on one
#: stream when another has simply gone quiet, whatever it is allowed to buffer.
#:
#: It is kept because a second is a sane bound on how long the muxer may sit on
#: a packet either way, and because ``MIPC_FFMPEG_ARGS`` can override it. It is
#: not what survives a stall. Nothing does; ``_async_watch`` ends one instead.
_MAX_INTERLEAVE_DELTA: Final = 1_000_000

#: How much input ffmpeg examines before it decides what the streams are.
#: ffmpeg's defaults are five seconds and five megabytes, which on a MIPC camera
#: costs thirteen seconds before the first packet reaches go2rtc — the AAC track
#: arrives slowly and ffmpeg waits for it. go2rtc holds the consumer for all of
#: that, and VLC times out long before, so the camera looks dead on the first
#: connection. A second is ample for h264 plus AAC, and cuts the launch to two.
_ANALYZE_DURATION: Final = 1_000_000
_PROBE_SIZE: Final = 500_000

#: The counters in ffmpeg's progress report that only move when a packet has
#: actually been delivered, and so are the only ones a stall watchdog may read.
#:
#: All three, because which of them is present depends on the run: ``frame`` is
#: reported only when the video is encoded, and copying it — the default, and
#: the point — leaves the key out altogether; ``total_size`` is ``N/A`` on the
#: RTSP muxer, which is the one this always uses. ``speed`` and ``bitrate`` move
#: with the wall clock and would report a dead stream as a healthy one.
_DELIVERED: Final = frozenset({b"frame", b"out_time_us", b"total_size"})

#: ``PR_SET_PDEATHSIG``, from ``linux/prctl.h``.
_PR_SET_PDEATHSIG: Final = 1

#: Opened here rather than in the child, because everything between ``fork`` and
#: ``exec`` has to be async signal safe and a ``dlopen`` is not.
_LIBC: Final = CDLL(None, use_errno=True)

#: How long ffmpeg is given to close its connections before it is killed.
#:
#: A TEARDOWN is one round trip to MIPC's relay, so this is not about how long
#: that takes — it is about ffmpeg noticing the signal at all. Measured against
#: a stalled input, it does not always: an ffmpeg blocked on an input that has
#: gone quiet sat through five seconds of SIGTERM and had to be killed.
#:
#: Three, because the whole shutdown has to fit inside the fifteen seconds
#: go2rtc allows before it kills this process instead. See
#: ``config.DEFAULT_STALL_TIMEOUT``.
_GRACE: Final = 3


def redact(line: bytes) -> bytes:
    """Replace every URL in a line of ffmpeg output with its scheme."""
    return _URL.sub(rb"\1://<redacted>", line)


def _die_with_parent() -> None:
    """Ask the kernel to end ffmpeg the moment this process ends.

    go2rtc stops a stream by cancelling the context its command runs under,
    which is a SIGKILL nothing here can catch or pass on. The ffmpeg left
    behind by one does not stop: it is blocked reading a socket MIPC's relay
    keeps answering, so its own read timeout never fires, and MIPC goes on
    counting it as a viewer. A few of those and the camera refuses the next
    session, which looks like the camera being down and clears only when
    someone power cycles it.

    ``PR_SET_PDEATHSIG`` is the only thing that outlives a SIGKILL of the
    parent, and it asks for a SIGKILL rather than the SIGTERM that would let
    ffmpeg send its TEARDOWN. This is the backstop, not the usual path: it is
    reached only when something has already killed this process outright, and
    an ffmpeg that is wedged enough to have got here is exactly the one that
    would ignore a polite signal too. Closing the socket is a weaker hint to
    MIPC than a TEARDOWN, but it is one it cannot miss. The graceful ending is
    ``_async_stop``'s job, and it gets there first whenever anyone is asking.

    Runs between fork and exec, so it does the one syscall and nothing else.
    ``prctl`` is Linux only, which the container always is; on anything else
    this is a no-op and ffmpeg can still be orphaned.
    """
    with suppress(AttributeError):
        _LIBC.prctl(_PR_SET_PDEATHSIG, SIGKILL)


def _accepted_media(settings: Settings) -> tuple[str, ...]:
    """Say which of the tracks MIPC announces are worth setting up.

    Refusing the audio at this layer, rather than dropping it later with
    ``-an``, is the point: ``-an`` still negotiates the RTP session and then
    waits for a track it is about to discard, which is the wait that used to
    make every connection take as long as the read timeout.
    """
    if settings.audio == "camera":
        return ()

    return ("-allowed_media_types", "video")


def _silence(settings: Settings) -> tuple[str, ...]:
    """Add a second input generating silence, when one is wanted.

    ``-re`` paces it at wall clock. Without it lavfi produces samples as fast
    as the CPU allows and the silence runs hours ahead of the video within
    seconds, which the RTSP muxer has no way to interleave.
    """
    if settings.audio != "silent":
        return ()

    return ("-f", "lavfi", "-re", "-i", _SILENCE)


def command(url: str, output: str, settings: Settings) -> list[str]:
    """Build the ffmpeg invocation that moves one camera into go2rtc.

    TCP is asked for in both directions: MIPC's relay drops UDP often enough to
    matter, and go2rtc's own RTSP server is a loopback hop where UDP buys
    nothing.
    """
    return [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
        # Not for a human to read: this is the only thing ffmpeg will say about
        # whether packets are still moving. It is parsed and dropped, never
        # echoed — but it carries no URL either way.
        "-progress",
        "pipe:1",
        "-rtsp_transport",
        "tcp",
        "-timeout",
        str(settings.read_timeout * _MICROSECONDS),
        "-fflags",
        "nobuffer",
        "-analyzeduration",
        str(_ANALYZE_DURATION),
        "-probesize",
        str(_PROBE_SIZE),
        *_accepted_media(settings),
        "-i",
        url,
        *_silence(settings),
        "-max_interleave_delta",
        str(_MAX_INTERLEAVE_DELTA),
        *settings.output_args,
        "-rtsp_transport",
        "tcp",
        "-f",
        "rtsp",
        output,
    ]


async def async_resolve(serial: str, settings: Settings) -> str:
    """Ask MIPC for a URL for one camera, and let go of the session.

    The session is closed straight away on purpose. It expires seconds from now
    regardless, and holding it open for the hours the stream may last would keep
    credentials in memory for no benefit.
    """
    client: MipcClient = create_client(settings.credentials)
    try:
        return await client.async_get_stream_url(serial, profile=settings.profile)
    finally:
        await client.async_close()


async def _relay(stderr: StreamReader) -> None:
    """Copy ffmpeg's diagnostics to ours, with the URLs taken out."""
    async for line in stderr:
        sys.stderr.buffer.write(redact(line))
        sys.stderr.buffer.flush()


async def _async_watch(stdout: StreamReader, process: Process, timeout: int) -> None:
    """End ffmpeg once it has gone ``timeout`` seconds without delivering.

    The failure this exists for is a camera that stops sending while its socket
    stays up. Neither of the other two watchdogs sees it: ffmpeg's read timeout
    is satisfied by MIPC's relay answering keepalives, and go2rtc's fifteen
    seconds are counted against a producer that has stopped writing, which is
    the same fifteen seconds — but go2rtc's way of acting on them is to SIGKILL
    this process, leaving the ffmpeg holding the MIPC session behind. Ending it
    here, first, is what makes the difference between a restart and a leak.

    Reading the progress report is also what keeps ffmpeg writing it: the pipe
    is small, and a full one blocks ffmpeg for good.
    """
    delivered: dict[bytes, bytes] = {}
    deadline: float | None = None
    loop = get_running_loop()

    while True:
        try:
            # Nothing has been reported yet, so nothing has stalled yet: until
            # the output is open, startup belongs to the read timeout.
            wait = None if deadline is None else deadline - loop.time()
            line = await wait_for(stdout.readline(), wait)
        except TimeoutError:
            LOGGER.error(
                "Nothing delivered for %ss; restarting the stream. The camera or "
                "MIPC's relay stopped sending while the connection stayed up",
                timeout,
            )
            await _async_stop(process)

            return

        if not line:
            return

        key, _, value = line.strip().partition(b"=")
        if key in _DELIVERED and delivered.get(key) != value:
            delivered[key] = value
            deadline = loop.time() + timeout


async def _async_stop(process: Process) -> None:
    """Ask ffmpeg to stop, and insist if it will not.

    The polite signal first, because an ffmpeg that answers it tears the RTSP
    session down on the way out and MIPC frees the camera's viewer slot there
    and then. One blocked on an input that has gone quiet may not answer at
    all, and a stall is exactly when that happens, so the wait is short and
    what follows it is not a request.
    """
    _terminate(process)

    try:
        await wait_for(process.wait(), _GRACE)
    except TimeoutError:
        LOGGER.warning("ffmpeg ignored the signal for %ss; killing it", _GRACE)
        with suppress(ProcessLookupError):
            process.kill()


async def async_run(serial: str, output: str, settings: Settings) -> int:
    """Stream one camera into go2rtc until ffmpeg stops. Returns its exit code.

    go2rtc ends a stream by killing this process, so the signals it sends are
    passed on: ffmpeg gets to close its connections rather than leaving MIPC
    holding a session it thinks is still watching.
    """
    url = await async_resolve(serial, settings)

    process = await create_subprocess_exec(
        *command(url, output, settings),
        stdin=DEVNULL,
        stdout=PIPE,
        stderr=PIPE,
        preexec_fn=_die_with_parent,
    )

    loop = get_running_loop()
    for signal in (SIGTERM, SIGINT):
        with suppress(NotImplementedError):
            loop.add_signal_handler(signal, _terminate, process)

    # Both pipes were asked for just above. They are drained together because
    # either one filling up would wedge ffmpeg, and because the watchdog ends
    # only when ffmpeg does.
    with suppress(CancelledError):
        await gather(
            _relay(process.stderr),
            _async_watch(process.stdout, process, settings.stall_timeout),
        )

    return await process.wait()


def _terminate(process: Process) -> None:
    """Ask ffmpeg to stop, ignoring a process that already has."""
    with suppress(ProcessLookupError):
        process.terminate()

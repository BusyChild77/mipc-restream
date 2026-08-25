"""Mint one stream URL and hand it to ffmpeg, for the life of one connection.

This is what go2rtc runs per consumer. It exists as a wrapper around ffmpeg
rather than as a plain ffmpeg command line for two reasons: the URL is only
knowable a moment before it is used, and ffmpeg prints whatever URL it was given
into its own error messages, which would put a bearer token in the logs.
"""

from __future__ import annotations

import re
import sys
from asyncio import (
    CancelledError,
    StreamReader,
    create_subprocess_exec,
    get_running_loop,
)
from asyncio.subprocess import Process
from contextlib import suppress
from signal import SIGINT, SIGTERM
from subprocess import DEVNULL, PIPE
from typing import Final

from mipc_client import MipcClient, create_client

from .config import Settings

__all__ = ["async_run", "command", "redact"]

#: Any URL is treated as a secret. The MIPC one carries a single use token, and
#: nothing else ffmpeg prints a URL for is worth leaking either. The last
#: character may not be punctuation, so that the ``:`` in ffmpeg's usual
#: ``<url>: Connection refused`` survives and the message still reads.
_URL: Final = re.compile(rb"\b([a-z][a-z0-9+.-]*)://[^\s'\"]*[^\s'\".,:;!?]")

#: Read timeout, in microseconds, so a silent upstream ends the process instead
#: of holding a monitor open on a stream that stopped arriving.
#:
#: It is also, unavoidably, the startup cost. ffmpeg waits out this whole
#: timeout during RTSP setup before it decides it has seen everything MIPC is
#: going to announce, so the first frame is never sooner than this. Measured
#: against one camera, time to first output tracked the option almost exactly:
#: 15s gave 15.5s, 5s gave 5.4s, and omitting it gave 17.3s. go2rtc makes the
#: consumer wait for all of it, which is what made VLC and Shinobi give up.
#:
#: Five seconds is the compromise, and MIPC_READ_TIMEOUT moves it: lower starts
#: faster but calls a stalled upstream dead sooner, and every restart is a fresh
#: MIPC session.
_MICROSECONDS: Final = 1_000_000

#: How much input ffmpeg examines before it decides what the streams are.
#: ffmpeg's defaults are five seconds and five megabytes, which on a MIPC camera
#: costs thirteen seconds before the first packet reaches go2rtc — the AAC track
#: arrives slowly and ffmpeg waits for it. go2rtc holds the consumer for all of
#: that, and VLC times out long before, so the camera looks dead on the first
#: connection. A second is ample for h264 plus AAC, and cuts the launch to two.
_ANALYZE_DURATION: Final = 1_000_000
_PROBE_SIZE: Final = 500_000


def redact(line: bytes) -> bytes:
    """Replace every URL in a line of ffmpeg output with its scheme."""
    return _URL.sub(rb"\1://<redacted>", line)


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
        "-i",
        url,
        *settings.ffmpeg_args,
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


async def async_run(serial: str, output: str, settings: Settings) -> int:
    """Stream one camera into go2rtc until ffmpeg stops. Returns its exit code.

    go2rtc ends a stream by killing this process, so the signals it sends are
    passed on: ffmpeg gets to close its connections rather than leaving MIPC
    holding a session it thinks is still watching.
    """
    url = await async_resolve(serial, settings)

    process = await create_subprocess_exec(
        *command(url, output, settings), stdin=DEVNULL, stdout=DEVNULL, stderr=PIPE
    )

    loop = get_running_loop()
    for signal in (SIGTERM, SIGINT):
        with suppress(NotImplementedError):
            loop.add_signal_handler(signal, _terminate, process)

    # stderr is a pipe because it was asked for one just above.
    with suppress(CancelledError):
        await _relay(process.stderr)

    return await process.wait()


def _terminate(process: Process) -> None:
    """Ask ffmpeg to stop, ignoring a process that already has."""
    with suppress(ProcessLookupError):
        process.terminate()

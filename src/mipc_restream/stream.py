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
#: This used to double as the startup cost: time to first output tracked the
#: option almost exactly — 15s gave 15.5s, 5s gave 5.4s, omitting it gave 17.3s
#: — because ffmpeg sat in its stream probe waiting for MIPC's AAC track and
#: only the socket timeout ever ended the wait. That forced the timeout down to
#: five seconds, which is far too short a fuse for a path across the internet:
#: an ordinary relay stall killed the stream, and go2rtc had to mint a new
#: session while the recorder showed black.
#:
#: Refusing the audio track at the RTSP layer is what separated them, so the
#: timeout is now only a watchdog and can afford to be patient. See
#: ``config.AUDIO_MODES``.
_MICROSECONDS: Final = 1_000_000

#: A track of silence, so an output always has audio to offer even when the
#: camera's own is never asked for. Mono at 16 kHz because nothing listens to
#: it; it exists so a recorder's ``-map 0:a`` finds something.
_SILENCE: Final = "anullsrc=channel_layout=mono:sample_rate=16000"

#: How long the muxer may hold a packet back to interleave it, in microseconds.
#:
#: go2rtc gives a producer fifteen seconds to send something and then tears the
#: stream down. That number is hardcoded and unreachable from here: the
#: ``?timeout=`` it reads off an ANNOUNCE is parsed only after the stream name
#: is looked up, and an ``exec:`` source announces to a hash that is not a
#: stream name. So fifteen seconds is a ceiling on any stall this can survive,
#: whatever ``MIPC_READ_TIMEOUT`` says — and the symptom is go2rtc's own
#: ``read tcp ... i/o timeout``, not anything ffmpeg prints.
#:
#: In ``silent`` mode there is a second input running at wall clock, and that is
#: what keeps the connection warm while the camera's video is stalled — but only
#: once the muxer stops queueing it. ffmpeg holds a sparse stream back for ten
#: seconds by default, which leaves five to spare; a second leaves fourteen, and
#: puts the read timeout back in charge of deciding when a stall is a death.
#:
#: It costs nothing in the other modes: with one stream the muxer always has a
#: packet for every stream it knows about, and never queues anything at all.
_MAX_INTERLEAVE_DELTA: Final = 1_000_000

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

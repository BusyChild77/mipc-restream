"""Settings, read from the environment the container is started with.

Everything is an environment variable because that is what Container Manager
gives a project: a compose file and an ``.env`` beside it. Nothing here is read
from a file, so the image carries no configuration and no credentials.
"""

from __future__ import annotations

from dataclasses import dataclass
from os import environ
from shlex import split
from typing import Final

from mipc_client import MipcCredentials

from .exceptions import ConfigurationError

__all__ = ["Settings"]

#: What to do about the camera's audio track.
#:
#: MIPC delivers the AAC track slowly enough that ffmpeg's stream probe blocks
#: on it until the socket read timeout expires — which is why time to first
#: frame used to track ``MIPC_READ_TIMEOUT`` almost exactly, and why the timeout
#: had to stay small enough to be a poor watchdog. Refusing the track at the
#: RTSP layer removes that wait, and with it the reason the two were tied.
#:
#: ``silent`` is the default rather than ``none`` because an NVR set to record
#: sound emits ``-map 0:a``, which fails outright on a stream that has no audio
#: track: Shinobi exits with "Stream map '0:a' matches no streams" and never
#: records at all. A synthesised silent track costs a rounding error of CPU and
#: keeps that recorder running.
#:
#: ``camera`` restores MIPC's real audio for whoever wants it, and restores the
#: slow start with it: in that mode the read timeout is the startup cost again.
AUDIO_MODES: Final = ("silent", "camera", "none")

DEFAULT_AUDIO: Final = AUDIO_MODES[0]

#: Copying is the point: the camera already encoded the video, and a NAS should
#: not spend its CPU encoding it again. Only the silence is ever encoded, and
#: only because a track of silence has to come from somewhere.
DEFAULT_FFMPEG_ARGS: Final = {
    "silent": "-map 0:v -map 1:a -c:v copy -c:a aac -shortest",
    "camera": "-c copy",
    "none": "-c copy",
}

#: Profiles MIPC offers, largest first.
PROFILES: Final = ("p0", "p1", "p2", "p3")

_DEFAULT_PORTS: Final = {"rtsp": 8554, "api": 1984, "webrtc": 8555}

#: Seconds ffmpeg waits on a silent socket before giving up on it.
#:
#: Thirty, not five, because this is a watchdog on a path that crosses the
#: internet: a MIPC relay that stalls for six seconds is having a bad moment,
#: not dying, and killing the stream over it costs a fresh login, a fresh URL,
#: and a gap the recorder shows as a black screen. It is only affordable
#: because the audio track no longer makes this the startup cost too — see
#: ``AUDIO_MODES``.
#:
#: It is a socket timeout and nothing more. MIPC's relay answers ffmpeg's RTSP
#: keepalives whether or not the camera behind it is still sending video, so a
#: camera that stops while the relay keeps talking never trips this at all.
#: ``MIPC_STALL_TIMEOUT`` is the one that catches that.
DEFAULT_READ_TIMEOUT: Final = 30

#: Seconds without a delivered packet before the stream is restarted.
#:
#: Ten, because go2rtc gives a producer fifteen — hardcoded in
#: ``pkg/rtsp.Conn.Handle`` for a passive producer, and unreachable from here:
#: the ``?timeout=`` it reads off an ANNOUNCE is parsed only after the stream
#: name is looked up, and an ``exec:`` source announces to a hash that is not a
#: stream name. So fifteen seconds is the ceiling on any stall this can be
#: allowed to ride out, and there is no point setting this above it.
#:
#: Ten rather than fourteen because noticing the stall is only the first half:
#: ffmpeg then gets ``stream._GRACE`` seconds to close its session before it is
#: killed, and the two together have to fit inside the fifteen. Firing first is
#: the whole point — go2rtc acts on its own ceiling by cancelling the context
#: this process runs under, which is a SIGKILL, while ending ffmpeg from here
#: means it sends MIPC a TEARDOWN and the camera gets its viewer slot back.
DEFAULT_STALL_TIMEOUT: Final = 10

#: MIPC's answer when the account itself is not signed in to its cloud. It comes
#: back from the login call, ahead of any look at the password, so it says
#: nothing about the credentials being right or wrong.
#:
#: What it *does* say depends on what the account is, which is why it lives here
#: rather than beside the rest of the MIPC vocabulary: it is only readable next
#: to :meth:`Settings.is_device_account`.
OFFLINE_CODE: Final = "accounts.user.offline"


def _port(environment: dict[str, str], name: str, default: int) -> int:
    """Read a port number, refusing anything that is not one."""
    raw = environment.get(name, "").strip()
    if not raw:
        return default

    if not raw.isdigit() or not 1 <= int(raw) <= 65535:
        raise ConfigurationError(f"{name} is not a port number: {raw!r}")

    return int(raw)


def _choice(
    environment: dict[str, str], name: str, allowed: tuple[str, ...], default: str
) -> str:
    """Read one of a fixed set of words, listing them when it is not one."""
    raw = environment.get(name, "").strip().lower()
    if not raw:
        return default

    if raw not in allowed:
        raise ConfigurationError(f"{name} must be one of {', '.join(allowed)}: {raw!r}")

    return raw


def _seconds(environment: dict[str, str], name: str, default: int) -> int:
    """Read a timeout in whole seconds, refusing anything that is not one."""
    raw = environment.get(name, "").strip()
    if not raw:
        return default

    if not raw.isdigit() or int(raw) < 1:
        raise ConfigurationError(f"{name} is not a number of seconds: {raw!r}")

    return int(raw)


@dataclass(frozen=True, slots=True)
class Settings:
    """Everything the restreamer needs to know to run."""

    username: str
    password: str
    rtsp_port: int = _DEFAULT_PORTS["rtsp"]
    api_port: int = _DEFAULT_PORTS["api"]
    webrtc_port: int = _DEFAULT_PORTS["webrtc"]
    profile: str = PROFILES[0]
    #: Cameras to publish; empty means every camera on the account.
    serials: tuple[str, ...] = ()
    #: What ffmpeg is told to do with the streams it found. Empty means the
    #: default for the audio mode, which is why this is not resolved here: the
    #: two are chosen together and a dataclass default cannot see a sibling.
    ffmpeg_args: tuple[str, ...] = ()
    log_level: str = "info"
    #: Seconds before a socket with nothing on it is given up on.
    read_timeout: int = DEFAULT_READ_TIMEOUT
    #: Seconds before a stream that has stopped delivering is restarted.
    stall_timeout: int = DEFAULT_STALL_TIMEOUT
    #: One of :data:`AUDIO_MODES`.
    audio: str = DEFAULT_AUDIO

    @classmethod
    def from_environment(cls, environment: dict[str, str] | None = None) -> Settings:
        """Build the settings, or say which variable is wrong."""
        source = dict(environ if environment is None else environment)

        username = source.get("MIPC_USERNAME", "").strip()
        password = source.get("MIPC_PASSWORD", "")
        if not username or not password:
            raise ConfigurationError("MIPC_USERNAME and MIPC_PASSWORD must both be set")

        return cls(
            username=username,
            password=password,
            rtsp_port=_port(source, "MIPC_RTSP_PORT", _DEFAULT_PORTS["rtsp"]),
            api_port=_port(source, "MIPC_API_PORT", _DEFAULT_PORTS["api"]),
            webrtc_port=_port(source, "MIPC_WEBRTC_PORT", _DEFAULT_PORTS["webrtc"]),
            profile=_choice(source, "MIPC_STREAM_PROFILE", PROFILES, PROFILES[0]),
            serials=tuple(
                serial.strip()
                for serial in source.get("MIPC_SERIALS", "").split(",")
                if serial.strip()
            ),
            ffmpeg_args=tuple(split(source.get("MIPC_FFMPEG_ARGS", "").strip())),
            log_level=source.get("MIPC_LOG_LEVEL", "info").strip().lower() or "info",
            read_timeout=_seconds(source, "MIPC_READ_TIMEOUT", DEFAULT_READ_TIMEOUT),
            stall_timeout=_seconds(source, "MIPC_STALL_TIMEOUT", DEFAULT_STALL_TIMEOUT),
            audio=_choice(source, "MIPC_AUDIO", AUDIO_MODES, DEFAULT_AUDIO),
        )

    @property
    def credentials(self) -> MipcCredentials:
        """The account to authenticate with."""
        return MipcCredentials(self.username, self.password)

    @property
    def is_device_account(self) -> bool:
        """Whether the account is one camera's serial rather than an email address.

        MIPC lets a single camera be shared as an account of its own, and the
        two read :data:`OFFLINE_CODE` in opposite ways. On an address it means
        another sign-in displaced this session, and signing in again is exactly
        the fix. On a serial it means the camera is not connected to MIPC at
        all, and no amount of asking reaches a camera that is not there — which
        is the difference between a retry that is worth making and one that
        only spends the camera's session slots.

        An address is the only thing MIPC accepts that can contain an ``@``, so
        that is the whole test.
        """
        return "@" not in self.username

    @property
    def output_args(self) -> tuple[str, ...]:
        """What ffmpeg is told to do with the streams, once the mode is known.

        ``MIPC_FFMPEG_ARGS`` replaces this wholesale rather than adding to it,
        so whoever really does mean to transcode owns the mapping too: in
        ``silent`` mode the silence arrives as a second input, and an override
        that does not map it would silently publish video alone.
        """
        return self.ffmpeg_args or tuple(split(DEFAULT_FFMPEG_ARGS[self.audio]))

    def wanted(self, serial: str) -> bool:
        """Whether one camera is among those to publish."""
        return not self.serials or serial in self.serials

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

#: Copying is the point: the camera already encoded the video, and a NAS should
#: not spend its CPU encoding it again.
DEFAULT_FFMPEG_ARGS: Final = "-c copy"

#: Profiles MIPC offers, largest first.
PROFILES: Final = ("p0", "p1", "p2", "p3")

_DEFAULT_PORTS: Final = {"rtsp": 8554, "api": 1984, "webrtc": 8555}


def _port(environment: dict[str, str], name: str, default: int) -> int:
    """Read a port number, refusing anything that is not one."""
    raw = environment.get(name, "").strip()
    if not raw:
        return default

    if not raw.isdigit() or not 1 <= int(raw) <= 65535:
        raise ConfigurationError(f"{name} is not a port number: {raw!r}")

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
    ffmpeg_args: tuple[str, ...] = tuple(split(DEFAULT_FFMPEG_ARGS))
    log_level: str = "info"

    @classmethod
    def from_environment(cls, environment: dict[str, str] | None = None) -> Settings:
        """Build the settings, or say which variable is wrong."""
        source = dict(environ if environment is None else environment)

        username = source.get("MIPC_USERNAME", "").strip()
        password = source.get("MIPC_PASSWORD", "")
        if not username or not password:
            raise ConfigurationError("MIPC_USERNAME and MIPC_PASSWORD must both be set")

        profile = source.get("MIPC_STREAM_PROFILE", PROFILES[0]).strip() or PROFILES[0]
        if profile not in PROFILES:
            raise ConfigurationError(
                f"MIPC_STREAM_PROFILE must be one of {', '.join(PROFILES)}: {profile!r}"
            )

        return cls(
            username=username,
            password=password,
            rtsp_port=_port(source, "MIPC_RTSP_PORT", _DEFAULT_PORTS["rtsp"]),
            api_port=_port(source, "MIPC_API_PORT", _DEFAULT_PORTS["api"]),
            webrtc_port=_port(source, "MIPC_WEBRTC_PORT", _DEFAULT_PORTS["webrtc"]),
            profile=profile,
            serials=tuple(
                serial.strip()
                for serial in source.get("MIPC_SERIALS", "").split(",")
                if serial.strip()
            ),
            ffmpeg_args=tuple(
                split(source.get("MIPC_FFMPEG_ARGS", "").strip() or DEFAULT_FFMPEG_ARGS)
            ),
            log_level=source.get("MIPC_LOG_LEVEL", "info").strip().lower() or "info",
        )

    @property
    def credentials(self) -> MipcCredentials:
        """The account to authenticate with."""
        return MipcCredentials(self.username, self.password)

    def wanted(self, serial: str) -> bool:
        """Whether one camera is among those to publish."""
        return not self.serials or serial in self.serials

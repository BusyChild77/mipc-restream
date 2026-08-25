"""Generation of the go2rtc configuration that fronts the account's cameras.

go2rtc is the piece that makes this work at all: it runs one command per stream,
lazily, when the first consumer connects, and it fans that single upstream out
to every consumer that follows. So the camera is pulled from MIPC once no matter
how many recorders and dashboards are watching, and the command it runs is free
to mint a URL that would be stale a minute later.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping
from typing import Any, Final

from yaml import safe_dump, safe_load

from mipc_client import MipcDevice

from .config import Settings

__all__ = ["build", "merge", "parse", "render", "stream_name"]

#: Anything outside this is replaced, so a stream name is safe in a URL path.
_UNSAFE: Final = re.compile(r"[^a-z0-9]+")

#: How go2rtc is told to produce a stream. ``{output}`` is go2rtc's own
#: placeholder: it substitutes the address of the RTSP server it is listening on,
#: and the command is expected to push there.
_SOURCE: Final = "exec:mipc-restream stream {serial} {{output}}"


def _slug(value: str) -> str:
    """Reduce free text to ``[a-z0-9_]``.

    Accents are folded rather than dropped, so a camera called "Côté Jardin"
    becomes ``cote_jardin`` and not ``c_t_jardin``. Nicknames are whatever the
    owner typed into the MIPC app, in whatever language they think in.
    """
    folded = unicodedata.normalize("NFKD", value.casefold())
    ascii_only = folded.encode("ascii", "ignore").decode("ascii")

    return _UNSAFE.sub("_", ascii_only).strip("_")


def stream_name(device: MipcDevice, taken: Iterable[str] = ()) -> str:
    """Turn a camera's nickname into a name that reads well in a URL.

    Two cameras can carry the same nickname, and a nickname can be nothing but
    punctuation or nothing at all, so the serial is both the fallback and the
    tie breaker.
    """
    name = _slug(device.name) or _slug(device.serial)

    if name not in set(taken):
        return name

    return f"{name}_{_slug(device.serial)}"


def build(devices: Iterable[MipcDevice], settings: Settings) -> dict[str, Any]:
    """Build the go2rtc configuration for one account.

    Offline cameras are published too. MIPC reports a camera as offline whenever
    it last failed to reach it, which a camera on a flaky link does several times
    a day; leaving the stream out would mean Shinobi losing the monitor entirely
    rather than reconnecting when it comes back.
    """
    streams: dict[str, str] = {}
    for device in devices:
        if settings.wanted(device.serial):
            streams[stream_name(device, streams)] = _SOURCE.format(serial=device.serial)

    return {
        "log": {"level": settings.log_level},
        "api": {"listen": f":{settings.api_port}"},
        "rtsp": {"listen": f":{settings.rtsp_port}"},
        "webrtc": {"listen": f":{settings.webrtc_port}"},
        "streams": streams,
    }


def merge(config: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    """Lay a hand written overlay over the generated configuration.

    Generating the whole file means a camera renamed in the MIPC app is picked up
    on the next restart, but it also means anything hand written is overwritten.
    The overlay is the escape hatch: it wins, one key at a time, so adding a
    ``webrtc.candidates`` or a stream for a camera that is not on this account
    does not mean giving up generation.
    """
    merged = dict(config)
    for key, value in overlay.items():
        current = merged.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            merged[key] = merge(current, value)
        else:
            merged[key] = value

    return merged


def render(config: Mapping[str, Any]) -> str:
    """Serialise a configuration the way go2rtc reads it."""
    return safe_dump(dict(config), default_flow_style=False, sort_keys=False)


def parse(text: str) -> dict[str, Any]:
    """Read an overlay, treating an empty file as an empty overlay."""
    loaded = safe_load(text)

    return loaded if isinstance(loaded, dict) else {}

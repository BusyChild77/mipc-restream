"""Value objects exchanged with the MIPC cloud."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .exceptions import MipcProtocolError

__all__ = ["MipcCredentials", "MipcDevice"]

#: Value of ``stat`` meaning the camera is reachable.
_ONLINE = "Online"


@dataclass(frozen=True, slots=True)
class MipcCredentials:
    """The account a client authenticates with."""

    username: str
    password: str


@dataclass(frozen=True, slots=True)
class MipcDevice:
    """One camera, as listed by the MIPC account."""

    serial: str
    name: str
    online: bool
    model: str | None = None
    firmware: str | None = None

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> MipcDevice:
        """Build a device from one entry of the ``devs`` list."""
        serial = payload.get("sn")
        if not serial:
            raise MipcProtocolError("Device listing entry has no serial number")

        return cls(
            serial=serial,
            # The nickname is what the user named the camera in the MIPC app.
            name=payload.get("nick") or serial,
            online=payload.get("stat") == _ONLINE,
            model=payload.get("model") or None,
            firmware=payload.get("img_ver") or None,
        )

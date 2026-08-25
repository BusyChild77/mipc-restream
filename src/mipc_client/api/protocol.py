"""Decoding of the wire format MIPC answers with, and URL building.

Everything here is pure: no I/O, no state. That makes the odd shapes MIPC uses
easy to pin down in tests.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from json import JSONDecodeError, loads
from typing import Any, Final
from urllib.parse import urlencode

from ..const import Endpoint
from ..exceptions import MipcProtocolError, error_for

__all__ = ["MipcResponse", "MipcStatus", "build_url"]

#: Responses are JSONP: ``message({...});``.
_ENVELOPE: Final = re.compile(r"^\s*message\s*\((?P<body>.*)\)\s*;?\s*$", re.DOTALL)

#: Keys inside the payload are bare JavaScript identifiers, so they have to be
#: quoted before the body is valid JSON.
_BARE_KEY: Final = re.compile(
    r"(?P<open>[\{\[,])(?P<space>\s?)(?P<key>[a-z0-9_.]+)(?P<colon>:)", re.IGNORECASE
)

#: The two shapes MIPC reports a status with, as
#: ``(container, code key, sub code key, reason key)``.
_STATUS_SHAPES: Final = (
    ("ret", "code", "sub", "reason"),
    ("Result", "Code", "SubCode", "Reason"),
)


@dataclass(frozen=True, slots=True)
class MipcStatus:
    """Outcome MIPC reports next to the payload of a response."""

    code: str = ""
    sub_code: str = ""
    reason: str = ""

    @property
    def failed(self) -> bool:
        """Whether MIPC refused the request."""
        return bool(self.code or self.reason)

    @property
    def identifier(self) -> str:
        """Most specific code available, which is the one worth acting on."""
        return self.sub_code or self.code


@dataclass(frozen=True, slots=True)
class MipcResponse:
    """One decoded MIPC answer."""

    type: str
    data: dict[str, Any]

    @classmethod
    def decode(cls, raw: str) -> MipcResponse:
        """Decode a JSONP body, or raise :class:`MipcProtocolError`."""
        envelope = _ENVELOPE.match(raw)
        if envelope is None:
            raise MipcProtocolError("Response is not a MIPC JSONP envelope")

        try:
            payload = loads(
                _BARE_KEY.sub(r'\g<open>"\g<key>"\g<colon>', envelope["body"])
            )
        except JSONDecodeError as err:
            raise MipcProtocolError(f"Response body is not valid JSON: {err}") from err

        if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
            raise MipcProtocolError("Response has no data object")

        return cls(type=str(payload.get("type", "")), data=payload["data"])

    @property
    def status(self) -> MipcStatus:
        """Status reported alongside the payload, empty when MIPC reported none."""
        for container, code, sub_code, reason in _STATUS_SHAPES:
            block = self.data.get(container)
            if isinstance(block, Mapping):
                return MipcStatus(
                    code=str(block.get(code, "")),
                    sub_code=str(block.get(sub_code, "")),
                    reason=str(block.get(reason, "")),
                )

        # The gateway reports a bare string instead of a status object.
        result = self.data.get("result")

        return MipcStatus(code=result) if isinstance(result, str) else MipcStatus()

    def raise_on_error(self) -> None:
        """Raise the error matching the status MIPC reported, if it reported one."""
        status = self.status
        if status.failed:
            raise error_for(status.identifier, status.reason)

    def require(self, *path: str) -> Any:
        """Read a nested value, or raise :class:`MipcProtocolError` if absent."""
        value: Any = self.data
        for key in path:
            if not isinstance(value, Mapping) or key not in value:
                raise MipcProtocolError(f"Response is missing '{'.'.join(path)}'")
            value = value[key]

        return value


def build_url(host: str, endpoint: Endpoint, params: Mapping[str, Any]) -> str:
    """Build a request URL, percent encoding every value."""
    return f"{host.rstrip('/')}{endpoint.value}?{urlencode(params)}"

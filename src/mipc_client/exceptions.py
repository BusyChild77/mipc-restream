"""Errors raised by the MIPC client.

They derive from :class:`Exception` and from nothing framework specific on
purpose: this package knows nothing about whoever is calling it, so translating
these into a Home Assistant error, an exit code or an HTTP status is the
caller's job.
"""

from __future__ import annotations

from typing import Final

__all__ = [
    "MipcAuthenticationError",
    "MipcConnectionError",
    "MipcError",
    "MipcProtocolError",
    "MipcResponseError",
    "MipcSessionExpiredError",
    "error_for",
]

#: Codes meaning the credentials themselves are wrong; replaying will not help.
AUTHENTICATION_ERROR_CODES: Final = frozenset(
    {
        "accounts.mail.invalid",
        "accounts.pass.invalid",
        "accounts.user.inactive",
        "accounts.user.invalid",
        "accounts.user.unknown",
    }
)

#: Codes meaning the session went stale; re-authenticating fixes them.
SESSION_ERROR_CODES: Final = frozenset(
    {
        "accounts.lid.invalid",
        "accounts.nid.invalid",
        "accounts.sid.invalid",
        "accounts.user.offline",
        "ccms.session.invalid",
    }
)


class MipcError(Exception):
    """Base class for every error raised by the MIPC client."""


class MipcConnectionError(MipcError):
    """MIPC could not be reached, or did not answer in time."""


class MipcProtocolError(MipcError):
    """MIPC answered with something that is not a valid MIPC payload."""


class MipcResponseError(MipcError):
    """MIPC answered with an error payload."""

    def __init__(self, code: str, reason: str = "") -> None:
        """Keep the machine readable code next to its human readable reason."""
        super().__init__(f"{code}: {reason}" if reason else code)

        self.code = code
        self.reason = reason


class MipcAuthenticationError(MipcResponseError):
    """The username or the password is not accepted by MIPC."""


class MipcSessionExpiredError(MipcResponseError):
    """The session is no longer valid and has to be established again."""


def error_for(code: str, reason: str = "") -> MipcResponseError:
    """Build the most specific error matching the code MIPC sent back."""
    if code in AUTHENTICATION_ERROR_CODES:
        return MipcAuthenticationError(code, reason)
    if code in SESSION_ERROR_CODES:
        return MipcSessionExpiredError(code, reason)

    return MipcResponseError(code, reason)

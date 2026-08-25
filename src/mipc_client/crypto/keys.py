"""Diffie-Hellman key exchange used by MIPC, backed by the site's ``mdh.js``."""

from __future__ import annotations

from functools import cache

from .runtime import call

__all__ = ["DiffieHellman"]


@cache
def _parameters() -> tuple[str, str]:
    """Return the (prime, generator) pair hardcoded in ``mdh.js``."""
    parameters = call("parameters")

    return parameters["prime"], parameters["generator"]


class DiffieHellman:
    """Client half of the exchange that derives the key a session is signed with.

    The private key is generated on creation, so a new instance is exactly what
    a new session needs; the old one is dropped with the session it belonged to.
    """

    def __init__(self, private_key: str | None = None) -> None:
        """Generate a key pair, or rebuild one from a known private key."""
        self._private_key = (
            private_key if private_key is not None else call("gen_private")
        )
        self._public_key: str = call("gen_public", self._private_key)

    @property
    def prime(self) -> str:
        """Modulus MIPC expects to be told about in the exchange request."""
        return _parameters()[0]

    @property
    def generator(self) -> str:
        """Generator MIPC expects to be told about in the exchange request."""
        return _parameters()[1]

    @property
    def public_key(self) -> str:
        """Public key to send to MIPC."""
        return self._public_key

    def shared_secret(self, peer_public_key: str) -> str:
        """Derive the secret shared with MIPC from the key it answered with."""
        return call("gen_shared_secret", self._private_key, peer_public_key)

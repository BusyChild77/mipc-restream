"""Request signing used by MIPC, backed by the site's ``mcodec.js``."""

from __future__ import annotations

from .runtime import call

__all__ = ["generate_nid"]


def generate_nid(sequence: int, identifier: str, shared_secret: str, num: int) -> str:
    """Generate the NID that authenticates a single request.

    ``mcodec.nid`` takes two more optional values and the hash function to use;
    MIPC's own pages never fill the extra values in and always hash with MD5,
    so the runtime passes those for us.
    """
    return call("nid", sequence, identifier, shared_secret, num)

"""Password encryption expected by the MIPC login endpoint."""

from __future__ import annotations

from binascii import unhexlify
from hashlib import md5

from Crypto.Cipher import DES

__all__ = ["encrypt_password"]

#: MIPC encrypts with an all-zero initialisation vector.
_IV = bytes(8)


def _md5(value: str) -> str:
    """Hash as MIPC does: MD5 of the UTF-8 bytes, lowercase hex."""
    return md5(value.encode(), usedforsecurity=False).hexdigest()


def encrypt_password(password: str, shared_secret: str) -> str:
    """Encrypt a password for ``/cacs_login_req.js``.

    The MD5 of the password is the plaintext, and the first half of the MD5 of
    the shared secret is the DES key, both taken as raw bytes from their hex
    representation.
    """
    key = unhexlify(_md5(shared_secret)[:16])
    plaintext = unhexlify(_md5(password))

    return DES.new(key, DES.MODE_CBC, iv=_IV).encrypt(plaintext).hex()

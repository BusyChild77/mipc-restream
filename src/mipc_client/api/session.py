"""Lifecycle of an authenticated MIPC session.

One instance owns everything that is only valid until the session expires: the
host to talk to, the queue and session identifiers, the key the requests are
signed with, and the request counter.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from time import monotonic

from ..const import GATEWAY_URL, LOGGER, SESSION_TTL, Endpoint
from ..crypto import DiffieHellman, encrypt_password, generate_nid
from ..exceptions import MipcSessionExpiredError
from ..models import MipcCredentials
from .protocol import MipcResponse, build_url
from .transport import MipcTransport

__all__ = ["MipcSession"]

#: ``num`` argument the MIPC pages sign the login request with.
_LOGIN_NUM = 2


class MipcSession:
    """Establishes and holds an authenticated MIPC session."""

    def __init__(
        self,
        credentials: MipcCredentials,
        transport: MipcTransport,
        *,
        ttl: timedelta = SESSION_TTL,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        """Store the collaborators; nothing is contacted until the session is needed."""
        self._credentials = credentials
        self._transport = transport
        self._ttl = ttl.total_seconds()
        self._clock = clock

        self._host: str | None = None
        self._qid: str | None = None
        self._sid: str | None = None
        self._nid: str | None = None
        self._shared_secret: str | None = None
        self._sequence = 0
        self._expires_at = 0.0

    @property
    def authenticated(self) -> bool:
        """Whether a session is established and has not timed out."""
        return self._sid is not None and self._clock() < self._expires_at

    @property
    def host(self) -> str:
        """Host every call other than the gateway lookup goes to."""
        return self._require(self._host)

    @property
    def qid(self) -> str:
        """Queue identifier sent along with every authenticated call."""
        return self._require(self._qid)

    @property
    def sid(self) -> str:
        """Session identifier returned by the login call."""
        return self._require(self._sid)

    @property
    def nid(self) -> str:
        """Signature minted at login, which the listing calls reuse as is."""
        return self._require(self._nid)

    async def async_ensure(self) -> None:
        """Authenticate unless a live session is already available."""
        if not self.authenticated:
            await self.async_authenticate()

    async def async_authenticate(self) -> None:
        """Run the full handshake, replacing whatever session was held."""
        self.invalidate()

        await self._async_discover_host()
        await self._async_open_queue()
        shared_secret, lid = await self._async_exchange_keys()

        self._shared_secret = shared_secret
        self._nid = self.mint_nid(lid, _LOGIN_NUM)
        self._sid = await self._async_login(lid, self._nid)

        self.touch()
        LOGGER.debug("Authenticated with MIPC as %s", self._credentials.username)

    def invalidate(self) -> None:
        """Forget the session so the next call establishes a new one."""
        self._qid = None
        self._sid = None
        self._nid = None
        self._shared_secret = None
        self._expires_at = 0.0

    def touch(self) -> None:
        """Push the expiry back, after a call MIPC accepted."""
        self._expires_at = self._clock() + self._ttl

    def next_handle(self) -> int:
        """Take the next value of the counter MIPC echoes back as ``to_handle``."""
        self._sequence += 1

        return self._sequence

    def mint_nid(self, identifier: str, num: int) -> str:
        """Sign the request that is about to be built.

        Call this *before* taking the request's own handle: the MIPC pages sign
        with the counter value the previous request used, and the server checks
        the signature the same way.
        """
        return generate_nid(
            self._sequence, identifier, self._require(self._shared_secret), num
        )

    async def _async_discover_host(self) -> None:
        """Ask the fixed gateway which host serves this account."""
        if self._host is not None:
            return

        url = build_url(
            GATEWAY_URL, Endpoint.GATEWAY, {"hfrom_handle": self.next_handle()}
        )
        response = await self._transport.async_get_response(url)
        response.raise_on_error()

        self._host = str(response.require("server", "signal")[0])
        LOGGER.debug("MIPC host is %s", self._host)

    async def _async_open_queue(self) -> None:
        """Open a message queue and keep its identifier."""
        response = await self._async_call(Endpoint.SESSION, {})

        self._qid = str(response.require("qid"))

    async def _async_exchange_keys(self) -> tuple[str, str]:
        """Run the Diffie-Hellman exchange, returning the secret and the login id."""
        exchange = DiffieHellman()
        response = await self._async_call(
            Endpoint.KEY_EXCHANGE,
            {
                "dbnum_prime": exchange.prime,
                "dkey_a2b": exchange.public_key,
                "droot_num": exchange.generator,
            },
        )

        secret = exchange.shared_secret(str(response.require("key_b2a")))

        return secret, str(response.require("lid"))

    async def _async_login(self, lid: str, nid: str) -> str:
        """Hand over the encrypted credentials and keep the session identifier."""
        response = await self._async_call(
            Endpoint.LOGIN,
            {
                "hqid": self.qid,
                "dlid": lid,
                "dnid": nid,
                "duser": self._credentials.username,
                "dpass": encrypt_password(
                    self._credentials.password, self._require(self._shared_secret)
                ),
                "dsession_req": 1,
            },
        )

        return str(response.require("sid"))

    async def _async_call(
        self, endpoint: Endpoint, params: dict[str, object]
    ) -> MipcResponse:
        """Send one handshake request to the discovered host."""
        url = build_url(
            self.host, endpoint, {"hfrom_handle": self.next_handle(), **params}
        )
        response = await self._transport.async_get_response(url)
        response.raise_on_error()

        return response

    def _require[T](self, value: T | None) -> T:
        """Return a session value, or report that the session has to be renewed."""
        if value is None:
            raise MipcSessionExpiredError(
                "mipc.session.missing", "No live MIPC session"
            )

        return value

"""HTTP transport for MIPC, including the TLS quirks its gateway needs."""

from __future__ import annotations

from asyncio import to_thread
from collections.abc import Awaitable, Callable
from ssl import SSLContext
from threading import Lock
from typing import Any

from requests import RequestException, Response, Session
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context

from ..const import REQUEST_TIMEOUT, TLS_CIPHERS
from ..exceptions import MipcConnectionError
from .protocol import MipcResponse

__all__ = ["ExecutorJob", "MipcTransport"]

#: Runs a blocking callable in a worker thread and awaits its result.
#: :func:`asyncio.to_thread` has this shape, and so does Home Assistant's
#: ``hass.async_add_executor_job``; taking it as a dependency is what lets the
#: same client run under an event loop that already owns its own pool.
type ExecutorJob = Callable[..., Awaitable[Any]]


def _build_ssl_context() -> SSLContext:
    """Build the TLS settings MIPC's gateway needs.

    www.mipcm.com only negotiates TLS 1.2 with static RSA key exchange and
    refuses every forward secrecy suite, so those have to be asked for
    explicitly. Certificate verification stays on.
    """
    return create_urllib3_context(ciphers=TLS_CIPHERS)


class _MipcAdapter(HTTPAdapter):
    """Talks HTTPS with the cipher suites MIPC is limited to."""

    def __init__(self, ssl_context: SSLContext) -> None:
        """Keep the context to hand to every pool this adapter opens."""
        self._ssl_context = ssl_context

        super().__init__()

    def init_poolmanager(self, *args: Any, **kwargs: Any) -> None:
        """Open pools with the MIPC TLS settings."""
        kwargs["ssl_context"] = self._ssl_context

        return super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, *args: Any, **kwargs: Any) -> Any:
        """Reach proxies with the MIPC TLS settings too."""
        kwargs["ssl_context"] = self._ssl_context

        return super().proxy_manager_for(*args, **kwargs)


class MipcTransport:
    """Fetches MIPC URLs, off the event loop.

    A single :class:`requests.Session` is reused so connections are kept alive
    between calls. ``requests`` sessions are not thread safe and the executor is
    free to be a pool, so a lock makes sure only one request is in flight at a
    time, which is well within what any consumer here needs.
    """

    def __init__(
        self, executor: ExecutorJob | None = None, timeout: int = REQUEST_TIMEOUT
    ) -> None:
        """Store the executor to run blocking work on.

        Without one, each call gets a thread of its own from
        :func:`asyncio.to_thread`. Callers that already run an executor — Home
        Assistant being the one this was written for — should pass theirs.
        """
        self._executor = executor or to_thread
        self._timeout = timeout
        self._lock = Lock()
        self._session: Session | None = None

    async def async_get_response(self, url: str) -> MipcResponse:
        """Fetch a URL and decode the JSONP answer."""
        response = await self._executor(self._fetch, url)

        return MipcResponse.decode(response.text)

    async def async_get_bytes(self, url: str) -> bytes:
        """Fetch a URL and return its body untouched."""
        response = await self._executor(self._fetch, url)

        return response.content

    async def async_close(self) -> None:
        """Release the underlying connections."""
        await self._executor(self._close)

    def _fetch(self, url: str) -> Response:
        """Perform one request. Runs in a worker thread."""
        try:
            with self._lock:
                response = self._get_session().get(url, timeout=self._timeout)
                response.raise_for_status()

                return response
        except RequestException as err:
            raise MipcConnectionError(f"Request to MIPC failed: {err}") from err

    def _get_session(self) -> Session:
        """Return the shared session, building it on first use."""
        if self._session is None:
            session = Session()
            session.mount("https://", _MipcAdapter(_build_ssl_context()))
            self._session = session

        return self._session

    def _close(self) -> None:
        """Close the shared session. Runs in a worker thread."""
        with self._lock:
            if self._session is not None:
                self._session.close()
                self._session = None

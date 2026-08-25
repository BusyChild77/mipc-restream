"""The operations a consumer performs against a MIPC account."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from ..const import LOGGER, MAX_ATTEMPTS, MAX_DEVICES, STREAM_PROFILE, Endpoint
from ..exceptions import MipcAuthenticationError, MipcError
from ..models import MipcCredentials, MipcDevice
from .protocol import MipcResponse, build_url
from .session import MipcSession
from .transport import MipcTransport

__all__ = ["MipcClient"]

_T = TypeVar("_T")

#: ``num`` argument the MIPC pages sign per-device calls with.
_DEVICE_NUM = 0

#: Placeholder token the MIPC pages send with a snapshot request.
_SNAPSHOT_TOKEN = "p1_xxxxxxxxxx"


class MipcClient:
    """Reads cameras, stream URLs and snapshots from a MIPC account.

    Every call is replayed on a recoverable failure, after dropping the session
    so the next attempt starts from a fresh handshake. Wrong credentials are not
    recoverable and are raised straight away.
    """

    def __init__(
        self,
        credentials: MipcCredentials,
        transport: MipcTransport,
        *,
        session: MipcSession | None = None,
    ) -> None:
        """Wire the client to its transport and session."""
        self._transport = transport
        self._session = session or MipcSession(credentials, transport)

    async def async_authenticate(self) -> None:
        """Check the credentials by establishing a session."""
        await self._session.async_authenticate()

    async def async_get_devices(self) -> list[MipcDevice]:
        """List the cameras the account can see."""

        async def call() -> list[MipcDevice]:
            response = await self._async_call(
                Endpoint.DEVICES,
                {
                    "dsess": 1,
                    "dsess_nid": self._session.nid,
                    "dstart": 0,
                    "dcounts": MAX_DEVICES,
                },
            )

            # An account without any camera simply has no `devs` key.
            return [
                MipcDevice.from_payload(payload)
                for payload in response.data.get("devs", [])
            ]

        return await self._async_with_retry(call)

    async def async_get_stream_url(
        self, serial: str, profile: str = STREAM_PROFILE
    ) -> str:
        """Ask MIPC for an RTSP URL for one camera.

        The URL carries a single use token and is only valid for the few seconds
        the session lives, so it has to be fetched again for every playback.

        ``profile`` selects which encoding the camera sends: ``p0`` is the
        largest the MIPC web player offers and ``p1``..``p3`` are progressively
        smaller. It is worth lowering when the video crosses the internet twice,
        which it does whenever something restreams this.
        """

        async def call() -> str:
            # The signature is minted before the request takes its own handle.
            nid = self._session.mint_nid(self._session.sid, _DEVICE_NUM)
            response = await self._async_call(
                Endpoint.PLAY,
                {
                    "dsess": 1,
                    "dsess_nid": nid,
                    "dsess_sn": serial,
                    "dsetup": 1,
                    # The web player asks for RTP over HTTP. RTSP is asked for
                    # instead because every consumer here — a player, ffmpeg,
                    # Home Assistant's stream component — reads it directly.
                    "dsetup_stream": "RTSP",
                    "dsetup_trans": 1,
                    "dsetup_trans_proto": "rtsp",
                    "dtoken": profile,
                },
            )

            return str(response.require("MediaUri", "Uri"))

        return await self._async_with_retry(call)

    async def async_get_snapshot(self, serial: str) -> bytes:
        """Fetch a still image straight from one camera."""

        async def call() -> bytes:
            # The signature is minted before the request takes its own handle.
            nid = self._session.mint_nid(self._session.sid, _DEVICE_NUM)
            url = build_url(
                self._session.host,
                Endpoint.SNAPSHOT,
                {
                    "hfrom_handle": self._session.next_handle(),
                    "dsess": 1,
                    "dsess_nid": nid,
                    "dsess_sn": serial,
                    "dtoken": _SNAPSHOT_TOKEN,
                    "dencode_type": 0,
                    "dpic_types_support": 2,
                    "dflag": 2,
                },
            )
            image = await self._transport.async_get_bytes(url)
            self._session.touch()

            return image

        return await self._async_with_retry(call)

    async def async_close(self) -> None:
        """Release the resources held by the transport."""
        await self._transport.async_close()

    async def _async_call(
        self, endpoint: Endpoint, params: dict[str, Any]
    ) -> MipcResponse:
        """Send one authenticated call and hand back the decoded answer."""
        url = build_url(
            self._session.host,
            endpoint,
            {
                "hfrom_handle": self._session.next_handle(),
                "hqid": self._session.qid,
                **params,
            },
        )
        response = await self._transport.async_get_response(url)
        response.raise_on_error()
        self._session.touch()

        return response

    async def _async_with_retry(self, call: Callable[[], Awaitable[_T]]) -> _T:
        """Run a call, renewing the session and retrying on recoverable errors."""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                await self._session.async_ensure()

                return await call()
            except MipcAuthenticationError:
                # Replaying will not make the credentials any better.
                raise
            except MipcError as err:
                self._session.invalidate()
                if attempt == MAX_ATTEMPTS:
                    raise

                LOGGER.debug(
                    "MIPC call failed (attempt %s), retrying: %s", attempt, err
                )

        raise AssertionError("unreachable")  # pragma: no cover

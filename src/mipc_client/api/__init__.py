"""Client library for the MIPC cloud, kept independent from any consumer."""

from __future__ import annotations

from ..models import MipcCredentials
from .client import MipcClient
from .protocol import MipcResponse, MipcStatus, build_url
from .session import MipcSession
from .transport import ExecutorJob, MipcTransport

__all__ = [
    "ExecutorJob",
    "MipcClient",
    "MipcResponse",
    "MipcSession",
    "MipcStatus",
    "MipcTransport",
    "build_url",
    "create_client",
]


def create_client(
    credentials: MipcCredentials, executor: ExecutorJob | None = None
) -> MipcClient:
    """Build a client for one account.

    Pass ``executor`` to run the blocking HTTP work on a pool the caller already
    owns — ``hass.async_add_executor_job`` under Home Assistant. Left out, every
    call gets a thread of its own, which is what a standalone process wants.
    """
    return MipcClient(credentials, MipcTransport(executor))

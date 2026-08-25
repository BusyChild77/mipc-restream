"""A client for the MIPC cloud, which is where MIPC cameras keep their video.

There is no local device protocol here. Everything goes through MIPC's cloud,
which answers JSONP over plain HTTP and signs requests with scripts lifted from
its own website. Most of the odd-looking code exists because the server demands
it; the comments say which part was not our idea.
"""

from __future__ import annotations

from .api import (
    ExecutorJob,
    MipcClient,
    MipcResponse,
    MipcSession,
    MipcStatus,
    MipcTransport,
    build_url,
    create_client,
)
from .const import (
    GATEWAY_URL,
    MAX_ATTEMPTS,
    MAX_DEVICES,
    SESSION_TTL,
    STREAM_PROFILE,
    Endpoint,
)
from .exceptions import (
    MipcAuthenticationError,
    MipcConnectionError,
    MipcError,
    MipcProtocolError,
    MipcResponseError,
    MipcSessionExpiredError,
    error_for,
)
from .models import MipcCredentials, MipcDevice

__version__ = "2.0.0"

__all__ = [
    "GATEWAY_URL",
    "MAX_ATTEMPTS",
    "MAX_DEVICES",
    "SESSION_TTL",
    "STREAM_PROFILE",
    "Endpoint",
    "ExecutorJob",
    "MipcAuthenticationError",
    "MipcClient",
    "MipcConnectionError",
    "MipcCredentials",
    "MipcDevice",
    "MipcError",
    "MipcProtocolError",
    "MipcResponse",
    "MipcResponseError",
    "MipcSession",
    "MipcSessionExpiredError",
    "MipcStatus",
    "MipcTransport",
    "__version__",
    "build_url",
    "create_client",
    "error_for",
]

"""Constants describing the MIPC protocol.

Only values the wire format itself imposes live here. Anything that belongs to
a consumer — a polling interval, an integration domain — is that consumer's
business and is deliberately absent.
"""

from __future__ import annotations

from datetime import timedelta
from enum import StrEnum
from logging import Logger, getLogger
from typing import Final

LOGGER: Final[Logger] = getLogger(__package__)

#: Only entry point that is a fixed address; it hands out the host to use next.
GATEWAY_URL: Final = "https://www.mipcm.com:7443"


class Endpoint(StrEnum):
    """Paths this client talks to.

    MIPC exposes many more endpoints (PTZ control, disk, speaker, schedules...);
    they are documented in ``mipc-en.md`` and deliberately left out until a
    consumer actually needs them.
    """

    GATEWAY = "/cmipcgw/cmipcgw_get_req.js"
    SESSION = "/mmq_create.js"
    KEY_EXCHANGE = "/cacs_dh_req.js"
    LOGIN = "/cacs_login_req.js"
    DEVICES = "/ccm_devs_get.js"
    PLAY = "/ccm_play.js"
    SNAPSHOT = "/ccm_pic_get.jpg"


# www.mipcm.com only negotiates TLS 1.2 with static RSA key exchange, and refuses
# every forward secrecy suite. Recent Python releases no longer offer those suites
# by default, so the server answers the handshake with an alert instead of its
# certificate unless they are asked for explicitly. Certificates stay verified.
TLS_CIPHERS: Final = "AES256-GCM-SHA384:AES128-GCM-SHA256:DEFAULT:@SECLEVEL=1"

REQUEST_TIMEOUT: Final = 10
#: How many times a call is replayed on a recoverable error before giving up.
MAX_ATTEMPTS: Final = 3
#: MIPC drops an idle session after 30 seconds.
SESSION_TTL: Final = timedelta(seconds=30)

#: Stream profile requested from the camera. ``p0`` is the highest quality one
#: the MIPC web player offers; ``p1``..``p3`` are progressively smaller.
STREAM_PROFILE: Final = "p0"
#: Upper bound sent to the device listing endpoint.
MAX_DEVICES: Final = 1024

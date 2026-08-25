"""Publish MIPC cloud cameras as ordinary RTSP streams on the local network.

MIPC hands out a stream URL that is single use and dies with the session that
minted it, which nothing but a purpose-built client can follow. go2rtc runs a
command per consumer connection, so the URL is minted at exactly the moment it
is about to be used, and everything downstream — Shinobi, Home Assistant, VLC —
sees a stable RTSP address it can reconnect to for years.
"""

from __future__ import annotations

__version__ = "1.0.0"

__all__ = ["__version__"]

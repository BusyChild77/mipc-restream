"""Errors raised by the restreamer itself, as opposed to by the MIPC client."""

from __future__ import annotations

__all__ = ["ConfigurationError", "RestreamError"]


class RestreamError(Exception):
    """Base class for every error this package raises."""


class ConfigurationError(RestreamError):
    """The environment does not describe a runnable restreamer."""

"""Fixtures shared by the restreamer tests."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from mipc_client import MipcDevice
from mipc_client.crypto import runtime
from mipc_restream.config import Settings

SERIAL = "MIPC0000001"
OTHER_SERIAL = "MIPC0000002"


@pytest.fixture(scope="session", autouse=True)
def warm_javascript_runtime() -> None:
    """Start the QuickJS worker thread once, rather than inside a test."""
    runtime.call("parameters")


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Make sure a developer's own MIPC_* variables cannot reach a test."""
    for name in (
        "MIPC_USERNAME",
        "MIPC_PASSWORD",
        "MIPC_STREAM_PROFILE",
        "MIPC_SERIALS",
        "MIPC_RTSP_PORT",
        "MIPC_API_PORT",
        "MIPC_WEBRTC_PORT",
        "MIPC_FFMPEG_ARGS",
        "MIPC_LOG_LEVEL",
        "MIPC_READ_TIMEOUT",
        "MIPC_AUDIO",
    ):
        monkeypatch.delenv(name, raising=False)

    yield


@pytest.fixture
def credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Put a usable account in the environment."""
    monkeypatch.setenv("MIPC_USERNAME", "owner@example.com")
    monkeypatch.setenv("MIPC_PASSWORD", "s3cr3t")


@pytest.fixture
def settings() -> Settings:
    """Return settings built without touching the environment."""
    return Settings(username="owner@example.com", password="s3cr3t")


@pytest.fixture
def device() -> MipcDevice:
    """Return a camera the way MIPC lists one."""
    return MipcDevice(SERIAL, "Front Door", True, "f136", "v5.3.1")


@pytest.fixture
def devices(device: MipcDevice) -> list[MipcDevice]:
    """Return the two cameras the fake account holds."""
    return [device, MipcDevice(OTHER_SERIAL, "Back Gate", False)]

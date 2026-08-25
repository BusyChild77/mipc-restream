"""Tests for reading the settings out of the environment."""

from __future__ import annotations

import pytest

from mipc_restream.config import Settings
from mipc_restream.exceptions import ConfigurationError


def test_an_account_is_enough(credentials: None) -> None:
    """Everything but the credentials has a working default."""
    settings = Settings.from_environment()

    assert settings.credentials.username == "owner@example.com"
    assert settings.credentials.password == "s3cr3t"
    assert settings.rtsp_port == 8554
    assert settings.api_port == 1984
    assert settings.webrtc_port == 8555
    assert settings.profile == "p0"
    assert settings.serials == ()
    assert settings.ffmpeg_args == ("-c", "copy")
    assert settings.log_level == "info"


@pytest.mark.parametrize(
    "environment",
    [
        {},
        {"MIPC_USERNAME": "owner@example.com"},
        {"MIPC_PASSWORD": "s3cr3t"},
        {"MIPC_USERNAME": "  ", "MIPC_PASSWORD": "s3cr3t"},
    ],
)
def test_an_account_is_required(environment: dict[str, str]) -> None:
    """Half an account is not an account."""
    with pytest.raises(ConfigurationError, match="MIPC_USERNAME"):
        Settings.from_environment(environment)


def test_every_setting_can_be_overridden(credentials: None) -> None:
    """The environment is the only way anything is configured."""
    settings = Settings.from_environment(
        {
            "MIPC_USERNAME": "owner@example.com",
            "MIPC_PASSWORD": "s3cr3t",
            "MIPC_RTSP_PORT": "9554",
            "MIPC_API_PORT": "9984",
            "MIPC_WEBRTC_PORT": "9555",
            "MIPC_STREAM_PROFILE": "p2",
            "MIPC_SERIALS": "AAA, BBB ,",
            "MIPC_FFMPEG_ARGS": "-c:v copy -an",
            "MIPC_LOG_LEVEL": "DEBUG",
        }
    )

    assert settings.rtsp_port == 9554
    assert settings.api_port == 9984
    assert settings.webrtc_port == 9555
    assert settings.profile == "p2"
    assert settings.serials == ("AAA", "BBB")
    assert settings.ffmpeg_args == ("-c:v", "copy", "-an")
    assert settings.log_level == "debug"


@pytest.mark.parametrize("value", ["", "   "])
def test_blank_values_fall_back_to_the_defaults(value: str) -> None:
    """An `.env` with an empty line for a setting means "leave it alone"."""
    settings = Settings.from_environment(
        {
            "MIPC_USERNAME": "owner@example.com",
            "MIPC_PASSWORD": "s3cr3t",
            "MIPC_RTSP_PORT": value,
            "MIPC_STREAM_PROFILE": value,
            "MIPC_FFMPEG_ARGS": value,
            "MIPC_LOG_LEVEL": value,
        }
    )

    assert settings.rtsp_port == 8554
    assert settings.profile == "p0"
    assert settings.ffmpeg_args == ("-c", "copy")
    assert settings.log_level == "info"


@pytest.mark.parametrize("port", ["nope", "0", "65536", "-1", "8554.5"])
def test_ports_are_checked(port: str) -> None:
    """A typo in a port is worth a message rather than a stack trace at bind."""
    with pytest.raises(ConfigurationError, match="MIPC_RTSP_PORT"):
        Settings.from_environment(
            {
                "MIPC_USERNAME": "owner@example.com",
                "MIPC_PASSWORD": "s3cr3t",
                "MIPC_RTSP_PORT": port,
            }
        )


def test_the_profile_is_checked() -> None:
    """MIPC answers an unknown profile with an empty stream, so catch it here."""
    with pytest.raises(ConfigurationError, match="p0, p1, p2, p3"):
        Settings.from_environment(
            {
                "MIPC_USERNAME": "owner@example.com",
                "MIPC_PASSWORD": "s3cr3t",
                "MIPC_STREAM_PROFILE": "best",
            }
        )


def test_an_empty_serial_list_wants_every_camera(settings: Settings) -> None:
    """The common case is publishing the whole account."""
    assert settings.wanted("anything")


def test_a_serial_list_is_a_filter() -> None:
    """Naming serials leaves every other camera out."""
    settings = Settings(username="u", password="p", serials=("AAA",))

    assert settings.wanted("AAA")
    assert not settings.wanted("BBB")

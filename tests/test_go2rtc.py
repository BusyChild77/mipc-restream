"""Tests for the generated go2rtc configuration."""

from __future__ import annotations

import pytest

from mipc_client import MipcDevice
from mipc_restream import go2rtc
from mipc_restream.config import Settings

from .conftest import OTHER_SERIAL, SERIAL


def test_a_nickname_becomes_a_url_safe_name(device: MipcDevice) -> None:
    """ "Front Door" is what the owner called it; front_door is what URLs allow."""
    assert go2rtc.stream_name(device) == "front_door"


@pytest.mark.parametrize(
    ("nickname", "expected"),
    [
        ("Garden — East", "garden_east"),
        ("  Attic  ", "attic"),
        ("Caméra 2", "camera_2"),
        ("Côté Jardin", "cote_jardin"),
        ("!!!", "mipc0000001"),
        ("", "mipc0000001"),
    ],
)
def test_awkward_nicknames_still_produce_a_name(nickname: str, expected: str) -> None:
    """A nickname is free text, so it can be punctuation or nothing at all."""
    assert go2rtc.stream_name(MipcDevice(SERIAL, nickname, True)) == expected


def test_a_repeated_nickname_falls_back_to_the_serial() -> None:
    """Two cameras called the same thing must not collapse into one stream."""
    second = MipcDevice(OTHER_SERIAL, "Front Door", True)

    assert go2rtc.stream_name(second, ["front_door"]) == "front_door_mipc0000002"


def test_every_camera_gets_a_stream(
    devices: list[MipcDevice], settings: Settings
) -> None:
    """The account's cameras are what the configuration is generated from."""
    config = go2rtc.build(devices, settings)

    assert config["streams"] == {
        "front_door": (
            "exec:mipc-restream stream MIPC0000001 {output}#killsignal=15#killtimeout=5"
        ),
        "back_gate": (
            "exec:mipc-restream stream MIPC0000002 {output}#killsignal=15#killtimeout=5"
        ),
    }


def test_go2rtc_is_asked_to_end_a_stream_politely(
    device: MipcDevice, settings: Settings
) -> None:
    """The default is a SIGKILL, and a SIGKILL here orphans an ffmpeg.

    go2rtc cancels the context its command runs under, which nothing on the
    other side can catch: the wrapper dies without passing anything on and its
    ffmpeg keeps the MIPC session open. 15 is SIGTERM, which it can pass on.
    """
    source = go2rtc.build([device], settings)["streams"]["front_door"]

    assert source.endswith("#killsignal=15#killtimeout=5")
    assert source.index("{output}") < source.index("#")


def test_an_offline_camera_is_still_published(
    devices: list[MipcDevice], settings: Settings
) -> None:
    """Leaving it out would cost the recorder the monitor, not just the picture."""
    assert not devices[1].online
    assert "back_gate" in go2rtc.build(devices, settings)["streams"]


def test_the_serial_filter_is_honoured(devices: list[MipcDevice]) -> None:
    """Naming serials is how one camera is published and the rest are not."""
    settings = Settings(username="u", password="p", serials=(SERIAL,))

    assert list(go2rtc.build(devices, settings)["streams"]) == ["front_door"]


def test_the_listeners_come_from_the_settings() -> None:
    """Moving a port is an environment variable, not an edit to a file."""
    settings = Settings(
        username="u", password="p", rtsp_port=1, api_port=2, webrtc_port=3
    )
    config = go2rtc.build([], settings)

    assert config["rtsp"]["listen"] == ":1"
    assert config["api"]["listen"] == ":2"
    assert config["webrtc"]["listen"] == ":3"


def test_an_overlay_wins_key_by_key(
    devices: list[MipcDevice], settings: Settings
) -> None:
    """Generation must not mean giving up anything hand written."""
    config = go2rtc.build(devices, settings)

    merged = go2rtc.merge(
        config,
        {
            "webrtc": {"candidates": ["192.168.1.10:8555"]},
            "streams": {"doorbell": "rtsp://elsewhere/live"},
            "log": {"level": "debug"},
        },
    )

    assert merged["webrtc"] == {"listen": ":8555", "candidates": ["192.168.1.10:8555"]}
    assert merged["streams"]["doorbell"] == "rtsp://elsewhere/live"
    assert merged["streams"]["front_door"].startswith("exec:")
    assert merged["log"]["level"] == "debug"
    assert config["log"]["level"] == "info", "the original is left alone"


def test_a_rendered_configuration_reads_back(
    devices: list[MipcDevice], settings: Settings
) -> None:
    """What go2rtc is handed is what was built."""
    config = go2rtc.build(devices, settings)

    assert go2rtc.parse(go2rtc.render(config)) == config


@pytest.mark.parametrize("text", ["", "\n", "# just a comment\n"])
def test_an_empty_overlay_is_an_empty_overlay(text: str) -> None:
    """A commented out overlay file must not blow up the entrypoint."""
    assert go2rtc.parse(text) == {}


def test_a_generated_configuration_reads_back_to_what_it_publishes(
    devices: list[MipcDevice], settings: Settings
) -> None:
    """A start that cannot reach MIPC regenerates from this, so it must round trip."""
    config = go2rtc.build(devices, settings)

    assert go2rtc.published(config) == {
        "front_door": SERIAL,
        "back_gate": OTHER_SERIAL,
    }


def test_the_names_survive_the_round_trip(
    devices: list[MipcDevice], settings: Settings
) -> None:
    """Losing them would rename every monitor in the recorder."""
    config = go2rtc.build(devices, settings)
    read_back = [
        MipcDevice(serial=serial, name=name, online=False)
        for name, serial in go2rtc.published(config).items()
    ]

    assert go2rtc.build(read_back, settings) == config


def test_a_stream_this_did_not_write_is_not_claimed(settings: Settings) -> None:
    """An overlay's own camera is not on the account and must not be read back."""
    config = {
        "streams": {
            "doorbell": "rtsp://192.168.1.50/stream1",
            "several": ["exec:mipc-restream stream AAA {output}"],
        }
    }

    assert go2rtc.published(config) == {}

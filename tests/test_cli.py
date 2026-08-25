"""Tests for the command line, the way the entrypoint and go2rtc call it."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from mipc_client import MipcConnectionError, MipcDevice
from mipc_restream import cli, go2rtc

from .conftest import OTHER_SERIAL, SERIAL


@pytest.fixture
def account(devices: list[MipcDevice]) -> Any:
    """Make the MIPC client list the fake account, without any network."""
    client = AsyncMock()
    client.async_get_devices.return_value = devices

    with patch.object(cli, "create_client", return_value=client):
        yield client


async def test_discover_prints_the_name_each_camera_will_be_served_as(
    credentials: None, account: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """The point of the command is turning nicknames into stream names."""
    assert await cli.async_main(["discover"]) == 0

    printed = capsys.readouterr().out
    assert "front_door" in printed
    assert SERIAL in printed
    assert "online" in printed
    assert "OFFLINE" in printed


async def test_discover_says_so_when_nothing_matched(
    credentials: None,
    account: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A serial filter that matches nothing is a typo worth reporting."""
    monkeypatch.setenv("MIPC_SERIALS", "NOT-A-SERIAL")

    assert await cli.async_main(["discover"]) == 0
    assert "No cameras" in capsys.readouterr().out


async def test_config_goes_to_stdout_by_default(
    credentials: None, account: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """Printing it is how you check what the entrypoint is about to write."""
    assert await cli.async_main(["config"]) == 0

    config = go2rtc.parse(capsys.readouterr().out)
    assert set(config["streams"]) == {"front_door", "back_gate"}
    assert config["rtsp"]["listen"] == ":8554"


async def test_config_writes_the_file_the_entrypoint_hands_to_go2rtc(
    credentials: None, account: Any, tmp_path: Path
) -> None:
    """The directory is created, because /config starts out as a bare volume."""
    target = tmp_path / "nested" / "go2rtc.yaml"

    assert await cli.async_main(["config", "--output", str(target)]) == 0

    assert set(go2rtc.parse(target.read_text())["streams"]) == {
        "front_door",
        "back_gate",
    }


async def test_config_applies_an_overlay_when_there_is_one(
    credentials: None, account: Any, tmp_path: Path
) -> None:
    """The overlay is the escape hatch from a file that is regenerated."""
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text("webrtc:\n  candidates:\n    - 192.168.1.10:8555\n")
    target = tmp_path / "go2rtc.yaml"

    await cli.async_main(["config", "--output", str(target), "--overlay", str(overlay)])

    config = go2rtc.parse(target.read_text())
    assert config["webrtc"]["candidates"] == ["192.168.1.10:8555"]
    assert config["webrtc"]["listen"] == ":8555"


async def test_a_missing_overlay_is_not_an_error(
    credentials: None, account: Any, tmp_path: Path
) -> None:
    """Most installations never write one, and the path is always passed."""
    target = tmp_path / "go2rtc.yaml"

    assert (
        await cli.async_main(
            [
                "config",
                "--output",
                str(target),
                "--overlay",
                str(tmp_path / "absent.yaml"),
            ]
        )
        == 0
    )
    assert target.exists()


async def test_an_unreadable_config_volume_is_a_misconfiguration(
    credentials: None, account: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bind mount the container cannot read is not MIPC being unreachable.

    /config belongs to whoever made the directory on the host, so uid 1000 can
    easily be locked out of it. Exit 2 stops the container and says so; exit 1
    would have the entrypoint keep a stale file and blame the network.
    """

    def deny(*_: object, **__: object) -> bool:
        raise PermissionError(13, "Permission denied", "/config/go2rtc.overlay.yaml")

    monkeypatch.setattr(Path, "is_file", deny)

    assert (
        await cli.async_main(
            [
                "config",
                "--output",
                str(tmp_path / "go2rtc.yaml"),
                "--overlay",
                "/config/go2rtc.overlay.yaml",
            ]
        )
        == 2
    )


async def test_the_serial_filter_reaches_the_generated_configuration(
    credentials: None, account: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Publishing one camera of several is a supported thing to want."""
    monkeypatch.setenv("MIPC_SERIALS", OTHER_SERIAL)
    target = tmp_path / "go2rtc.yaml"

    await cli.async_main(["config", "--output", str(target)])

    assert list(go2rtc.parse(target.read_text())["streams"]) == ["back_gate"]


async def test_stream_runs_the_camera_at_the_configured_profile(
    credentials: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This is what go2rtc invokes, once per consumer that connects."""
    monkeypatch.setenv("MIPC_STREAM_PROFILE", "p1")
    runner = AsyncMock(return_value=0)

    with patch.object(cli, "async_run", runner):
        assert await cli.async_main(["stream", SERIAL, "rtsp://out/x"]) == 0

    serial, output, settings = runner.await_args.args
    assert (serial, output) == (SERIAL, "rtsp://out/x")
    assert settings.profile == "p1"


async def test_stream_takes_a_profile_override(credentials: None) -> None:
    """One heavy camera can be dropped a notch without moving the others."""
    runner = AsyncMock(return_value=0)

    with patch.object(cli, "async_run", runner):
        await cli.async_main(["stream", SERIAL, "rtsp://out/x", "--profile", "p3"])

    assert runner.await_args.args[2].profile == "p3"


async def test_stream_reports_ffmpegs_exit_code(credentials: None) -> None:
    """go2rtc decides what to do next from it, so it is passed straight through."""
    with patch.object(cli, "async_run", AsyncMock(return_value=8)):
        assert await cli.async_main(["stream", SERIAL, "rtsp://out/x"]) == 8


async def test_a_missing_account_is_its_own_exit_code(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The entrypoint can tell "you forgot the .env" from "MIPC is down"."""
    assert await cli.async_main(["discover"]) == 2
    assert "MIPC_USERNAME" in caplog.text


async def test_mipc_being_unreachable_is_reported_not_raised(
    credentials: None, account: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """The entrypoint keeps the previous configuration when this happens."""
    account.async_get_devices.side_effect = MipcConnectionError("down")

    assert await cli.async_main(["config"]) == 1
    assert "MIPC refused" in caplog.text


def test_main_runs_the_loop(credentials: None, account: Any) -> None:
    """`mipc-restream` is a synchronous entry point over an async program."""
    assert cli.main(["discover"]) == 0


def test_the_command_is_required() -> None:
    """Running it bare should say what it can do, not do nothing."""
    with pytest.raises(SystemExit):
        cli.parse([])

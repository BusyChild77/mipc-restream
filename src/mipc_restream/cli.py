"""The command line, which is both an operator's tool and go2rtc's child process.

``discover`` and ``config`` are run by a person, or by the container's entrypoint
at boot. ``stream`` is never typed by anyone: it is what the generated go2rtc
configuration invokes, once per consumer connection.
"""

from __future__ import annotations

import logging
import sys
from argparse import ArgumentParser, Namespace
from asyncio import run
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Final

from mipc_client import MipcDevice, MipcError, MipcResponseError, create_client

from . import __version__, go2rtc
from .config import PROFILES, Settings
from .exceptions import RestreamError
from .stream import async_run

__all__ = ["main"]

LOGGER = logging.getLogger("mipc_restream")

#: Exit codes. go2rtc restarts a stream whatever this is, so it only matters to
#: the entrypoint and to whoever is reading the logs.
_OK = 0
_FAILED = 1
_MISCONFIGURED = 2

#: MIPC's answer when the account itself is not signed in to its cloud. It
#: comes back from the login call, ahead of any look at the password, so it
#: says nothing about the credentials being right or wrong.
#:
#: A MIPC account can be an email address or a single camera's serial — the
#: latter is what the app offers as sharing a device. On a serial, this code
#: is how MIPC reports that the camera is not connected to its cloud, and no
#: number of retries reaches a camera that is not there. The client retries
#: it anyway, because on an email account the same code means the session was
#: displaced and signing in again is exactly the fix.
_OFFLINE: Final = "accounts.user.offline"


async def _async_devices(settings: Settings) -> list[MipcDevice]:
    """List the cameras this restreamer is meant to publish."""
    client = create_client(settings.credentials)
    try:
        devices = await client.async_get_devices()
    finally:
        await client.async_close()

    return [device for device in devices if settings.wanted(device.serial)]


async def _async_discover(settings: Settings) -> int:
    """Print what the account holds, and the name each camera would be served as."""
    devices = await _async_devices(settings)
    if not devices:
        print("No cameras on this account matched.")

        return _OK

    published: list[tuple[str, MipcDevice]] = []
    for device in devices:
        published.append(
            (go2rtc.stream_name(device, [name for name, _ in published]), device)
        )

    width = max(len(name) for name, _ in published)
    print(f"{'STREAM'.ljust(width)}  {'SERIAL'.ljust(16)}  STATUS   NAME")
    for name, device in published:
        status = "online" if device.online else "OFFLINE"
        print(
            f"{name.ljust(width)}  {device.serial.ljust(16)}  {status:7}  {device.name}"
        )

    return _OK


async def _async_config(settings: Settings, arguments: Namespace) -> int:
    """Generate the go2rtc configuration for the account.

    Everything touching /config is guarded, because /config is a bind mount: it
    belongs to whoever created the directory on the host, not to the uid the
    image sets. Letting the resulting ``PermissionError`` escape would exit 1,
    which the entrypoint reads as MIPC being unreachable — blaming the network
    for a chown, and keeping a stale configuration while it does.
    """
    config = go2rtc.build(await _async_devices(settings), settings)

    try:
        overlay: Path | None = arguments.overlay
        if overlay is not None and overlay.is_file():
            config = go2rtc.merge(config, go2rtc.parse(overlay.read_text()))
            LOGGER.info("Applied the overlay at %s", overlay)

        rendered = go2rtc.render(config)
        if arguments.output is None:
            sys.stdout.write(rendered)
        else:
            arguments.output.parent.mkdir(parents=True, exist_ok=True)
            arguments.output.write_text(rendered, encoding="utf-8")
            LOGGER.info(
                "Wrote %s stream(s) to %s", len(config["streams"]), arguments.output
            )
    except OSError as err:
        LOGGER.error(
            "Cannot use %s: %s. /config is a bind mount; it must be writable by "
            "the uid the container runs as (1000)",
            err.filename,
            err.strerror,
        )

        return _MISCONFIGURED

    return _OK


async def _async_stream(settings: Settings, arguments: Namespace) -> int:
    """Run one camera into go2rtc until ffmpeg stops."""
    if arguments.profile is not None:
        settings = replace(settings, profile=arguments.profile)

    LOGGER.info("Starting %s at profile %s", arguments.serial, settings.profile)

    return await async_run(arguments.serial, arguments.output, settings)


def parse(argv: Sequence[str] | None = None) -> Namespace:
    """Read the command line."""
    parser = ArgumentParser(prog="mipc-restream", description=__doc__.splitlines()[0])
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("discover", help="list the cameras on the account")

    config = commands.add_parser("config", help="generate the go2rtc configuration")
    config.add_argument(
        "-o", "--output", type=Path, default=None, help="write here instead of stdout"
    )
    config.add_argument(
        "--overlay",
        type=Path,
        default=None,
        help="YAML laid over the generated configuration, if it exists",
    )

    stream = commands.add_parser("stream", help="what go2rtc runs; not for humans")
    stream.add_argument("serial", help="serial number of the camera")
    stream.add_argument("output", help="RTSP address to push to, from go2rtc")
    stream.add_argument(
        "--profile", choices=PROFILES, default=None, help="override MIPC_STREAM_PROFILE"
    )

    return parser.parse_args(argv)


async def async_main(argv: Sequence[str] | None = None) -> int:
    """Run one command and return its exit code."""
    arguments = parse(argv)

    try:
        settings = Settings.from_environment()
    except RestreamError as err:
        LOGGER.error("%s", err)

        return _MISCONFIGURED

    logging.getLogger().setLevel(settings.log_level.upper())

    try:
        if arguments.command == "discover":
            return await _async_discover(settings)
        if arguments.command == "config":
            return await _async_config(settings, arguments)

        return await _async_stream(settings, arguments)
    except MipcError as err:
        # Never `%s` a URL: this is the layer where one could reach a log file.
        LOGGER.error("MIPC refused: %s", err)
        if isinstance(err, MipcResponseError) and err.code == _OFFLINE:
            LOGGER.error(
                "MIPC reports the account as offline, which is about the account "
                "and not the password. If MIPC_USERNAME is a camera's serial "
                "rather than an email address, MIPC is saying that camera is not "
                "connected to its cloud: check its power and its network. Nothing "
                "here can reach it until MIPC can."
            )

        return _FAILED


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point of the ``mipc-restream`` command."""
    logging.basicConfig(
        format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr, level="INFO"
    )

    return run(async_main(argv))

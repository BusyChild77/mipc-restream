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
from collections.abc import Iterable, Sequence
from dataclasses import replace
from json import dumps, loads
from pathlib import Path
from typing import Final

from yaml import YAMLError

from mipc_client import MipcDevice, MipcError, MipcResponseError, create_client

from . import __version__, go2rtc
from .config import OFFLINE_CODE, PROFILES, Settings
from .exceptions import RestreamError
from .stream import async_run

__all__ = ["main"]

LOGGER = logging.getLogger("mipc_restream")

#: Exit codes. go2rtc restarts a stream whatever this is, so it only matters to
#: the entrypoint and to whoever is reading the logs.
_OK = 0
_FAILED = 1
_MISCONFIGURED = 2

#: What the cache keeps about a camera. Not its status: that is true of the
#: moment it was written and stale by the time it is read, and ``go2rtc.build``
#: publishes an offline camera regardless.
_CACHED: Final = ("serial", "name")


def _remember(devices: Iterable[MipcDevice], path: Path | None) -> None:
    """Write down what the account holds, for a start MIPC is not there for."""
    if path is None:
        return

    listing = [{field: getattr(d, field) for field in _CACHED} for d in devices]
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(dumps(listing, indent=2), encoding="utf-8")
    except OSError as err:
        # Never fatal. The configuration this was meant to help write has just
        # been generated from MIPC itself, which is the better source anyway.
        LOGGER.warning("Could not write the camera cache at %s: %s", path, err)


def _cached(path: Path | None) -> list[MipcDevice]:
    """Read back the cameras last seen, or nothing if there is no usable record."""
    if path is None or not path.is_file():
        return []

    try:
        return [
            MipcDevice(serial=entry["serial"], name=entry["name"], online=False)
            for entry in loads(path.read_text(encoding="utf-8"))
        ]
    except (OSError, ValueError, TypeError, KeyError) as err:
        LOGGER.warning("Ignoring the unusable camera cache at %s: %s", path, err)

        return []


def _previously(path: Path | None) -> list[MipcDevice]:
    """Read the cameras back out of a configuration an earlier start wrote.

    The last resort, and the one that matters on the first start after a fix:
    a deployment that has never once reached MIPC has no cache to fall back on,
    and on a device account that is exactly the deployment whose camera was
    away every time it tried. Keeping the old file instead would mean the fix
    never runs; reading the cameras out of it and generating again means it
    does.

    The stream name is taken from the file rather than derived, so the recorder
    keeps the addresses it was pointed at. MIPC's nickname is lost until MIPC
    answers again, which only matters if a camera was renamed meanwhile.
    """
    if path is None or not path.is_file():
        return []

    try:
        serving = go2rtc.published(go2rtc.parse(path.read_text(encoding="utf-8")))
    except (OSError, YAMLError) as err:
        LOGGER.warning("Ignoring the unreadable configuration at %s: %s", path, err)

        return []

    return [
        MipcDevice(serial=serial, name=name, online=False)
        for name, serial in serving.items()
    ]


def _offline_advice(settings: Settings) -> str:
    """Say what MIPC means by an offline account, for this kind of account."""
    if settings.is_device_account:
        return (
            "MIPC_USERNAME is a camera's serial rather than an email address, so "
            "MIPC is saying that camera is not connected to its cloud: check its "
            "power and its network, and confirm in the phone app. Nothing here "
            "reaches a camera MIPC cannot reach, and asking faster only spends "
            "the few session slots it has for when it does come back."
        )

    return (
        "MIPC reports the account as offline, which is about the account and not "
        "the password: another sign-in displaced this session, and signing in "
        "again is the fix."
    )


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


async def _async_known(
    settings: Settings, cache: Path | None, previous: Path | None
) -> list[MipcDevice]:
    """List the cameras, falling back to the ones last seen.

    A configuration has to be generated even when MIPC will not say what the
    account holds, and it has to be generated by *this* build. Keeping the file
    that is already there looks like the same thing and is not: that file was
    written by whichever version last managed to reach MIPC, so a fix to how a
    stream is invoked never reaches the one deployment that needs it — the one
    whose camera is away at every boot, which on a device account is exactly
    the deployment that has been failing.

    Publishing a camera that is not answering costs nothing. go2rtc pulls from
    MIPC only when a consumer connects, so an absent camera costs the one
    stream that fails and no other.
    """
    try:
        devices = await _async_devices(settings)
    except MipcError as err:
        remembered = _cached(cache) or _previously(previous)
        if not remembered:
            raise

        LOGGER.warning(
            "MIPC would not say what the account holds (%s); generating from "
            "the %s camera(s) last seen instead",
            err,
            len(remembered),
        )

        return remembered

    _remember(devices, cache)

    return devices


async def _async_config(settings: Settings, arguments: Namespace) -> int:
    """Generate the go2rtc configuration for the account.

    Everything touching /config is guarded, because /config is a bind mount: it
    belongs to whoever created the directory on the host, not to the uid the
    image sets. Letting the resulting ``PermissionError`` escape would exit 1,
    which the entrypoint reads as MIPC being unreachable — blaming the network
    for a chown, and keeping a stale configuration while it does.
    """
    known = await _async_known(settings, arguments.cache, arguments.output)
    config = go2rtc.build(known, settings)

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
    config.add_argument(
        "--cache",
        type=Path,
        default=None,
        help="remember the camera list here, to generate when MIPC is away",
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
        if isinstance(err, MipcResponseError) and err.code == OFFLINE_CODE:
            LOGGER.error("%s", _offline_advice(settings))

        return _FAILED


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point of the ``mipc-restream`` command."""
    logging.basicConfig(
        format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr, level="INFO"
    )

    return run(async_main(argv))

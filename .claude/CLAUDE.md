# CLAUDE.md

Guidance for working in this repository, whether you are a person or a coding agent.

## What this is

A container that publishes the cameras on a [MIPC](https://www.mipcm.com/) cloud
account as ordinary RTSP streams on the local network, so an NVR such as
[Shinobi](https://shinobi.video/) can read them. Built to be deployed as a **Project in
Synology's Container Manager**: one folder, one `compose.yaml`, one `.env`.

## The problem it solves

MIPC hands out an RTSP URL that is **single use** and dies with the session that minted
it, seconds later. Nothing that keeps an address and reconnects to it — which is every
NVR — can use one.

go2rtc has the shape that fixes this: it runs a command **per consumer connection**, so
the URL is minted at the moment it is about to be used. This project is that command
(`mipc-restream stream`), plus the configuration that wires it up
(`mipc-restream config`).

The second property matters as much: go2rtc opens **one** upstream connection per
stream and fans it out to every consumer. Shinobi recording around the clock keeps the
stream warm, and Home Assistant attaching to the same address costs no second cloud
session and no second viewer slot on the camera. Anything that would open a second
upstream per consumer is a regression, not an optimisation.

## The vendored client

`src/mipc_client/` is a **generated copy** of the sibling
[`mipc-client`](../../mipc-client) repository. Do not edit anything under it; a patch
made there is lost at the next sync. It is a copy rather than a path dependency because
the Docker build context is this folder and nothing above it — Container Manager
uploads one directory.

```sh
./scripts/sync_client.sh            # refresh from ../mipc-client
./scripts/sync_client.sh --check    # report drift
```

The same copy is carried by
[`homeassistant-mipc-camera-v2`](../../homeassistant-mipc-camera-v2). A client change
that suits one consumer and breaks the other is not finished.

## Commands

```sh
python -m venv .venv && source .venv/bin/activate
pip install -e . -r requirements_dev.txt

pytest                                   # whole suite, coverage is on by default
pytest tests/test_stream.py -q --no-cov  # one module, fast

ruff check src tests
ruff format src tests

docker compose build && docker compose up -d
docker exec mipc-restream mipc-restream discover
docker exec mipc-restream mipc-restream config      # what the entrypoint would write
```

`asyncio_mode = auto` in `pytest.ini` is load-bearing: without it the bare
`async def test_*` functions are not collected at all. `.ruff.toml` and `.coveragerc`
both exclude the vendored client, which is linted and covered in its own repository.

## Layout

```
src/mipc_client/     GENERATED copy of ../mipc-client — do not edit
src/mipc_restream/
├── config.py        Settings, read from the environment and nowhere else
├── go2rtc.py        generating the configuration, and the overlay merge
├── stream.py        mint one URL, run ffmpeg, watch it, redact its output
├── cli.py           discover / config / stream
└── exceptions.py    RestreamError, ConfigurationError
docker/entrypoint.sh generate the config, then exec go2rtc
Dockerfile           python:3.13-slim + ffmpeg + a pinned go2rtc
compose.yaml         what Container Manager reads
```

## Rules that are not negotiable

1. **Never edit `src/mipc_client/`.** Change [`mipc-client`](../../mipc-client) and
   re-sync.
2. **Never log a stream URL.** It is a bearer token. ffmpeg quotes URLs back in its own
   error messages, which is the whole reason `stream.py` runs ffmpeg as a child and
   filters its stderr instead of `exec`ing it. Anything that bypasses `redact()` — an
   `exec`, a `capture_output=False`, a debug `print` — reintroduces the leak.
3. **The URL is minted per connection, never cached.** Reusing one across connections
   works exactly once and then fails in a way that looks like the camera is down.
4. **Configuration comes from the environment.** The image carries no configuration and
   no credentials, so it is safe to build anywhere and push nowhere.
5. **Do not add `pid: host` to the compose file.** The URL is visible in `ps` inside the
   container for the life of a connection; the container's own PID namespace is what
   makes that acceptable.
6. **Do not transcode by default.** `-c copy` exists because a NAS should not spend its
   CPU re-encoding what the camera already encoded. `MIPC_FFMPEG_ARGS` is there for
   whoever really means it.
7. **Coverage stays at 100%.** The suite is small enough that an uncovered line means a
   behaviour nobody described.

## Conventions

- Every module, class, method and test has a docstring. `ruff` enforces it (`D` rules).
- Type hints everywhere; `from __future__ import annotations` at the top of every module.
- Comments explain *why*, not what. Most of the odd shapes here are MIPC's fault or
  go2rtc's contract; say which.
- A blank line before a `return` that follows a block.

## When you change something

| Change | Also do this |
|---|---|
| Anything about the MIPC protocol | change it in [`mipc-client`](../../mipc-client), re-sync **both** consumers |
| A new setting | add it to `Settings`, to `.env.example`, and to the table in the README |
| The go2rtc configuration | check the overlay still merges over it, and that `parse(render(x)) == x` |
| The Dockerfile | remember Container Manager builds without buildx, so `TARGETARCH` is unset |
| The go2rtc version | bump `GO2RTC_VERSION`; pass `GO2RTC_SHA256` to have it verified |
| A new dependency | add it to `pyproject.toml`; it must have a manylinux wheel or the image needs a compiler |

## Things that are still unknown

- **How often a MIPC stream stalls, and why.** That it *does* is settled: the
  camera stops sending while the relay keeps answering keepalives, which is why
  `MIPC_STALL_TIMEOUT` exists. Whether it correlates with the profile, the time of
  day or the uplink is not known. Measurements go in the README.
- **How many sessions a camera allows before it refuses the next one.** Enough
  leaked ffmpegs used to wedge one until it was power cycled, which is what
  `PR_SET_PDEATHSIG` and `#killsignal=15` are there to stop. The actual limit was
  never counted.
- **Whether MIPC rate-limits many sessions from one account.** One connection per
  camera is what this arranges for; several accounts' worth is untested.

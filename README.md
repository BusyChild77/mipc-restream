# mipc-restream

Publishes the cameras on a [MIPC](https://www.mipcm.com/) cloud account as
ordinary RTSP streams on your own network, so an NVR such as
[Shinobi](https://shinobi.video/) — or Home Assistant, or VLC — can read them
like any other IP camera.

Built to be deployed as a **Project in Synology's Container Manager**: upload the
folder, fill in `.env`, press build.

## Why this exists

MIPC will hand out an RTSP URL for a camera, but that URL is single use and dies
with the session that minted it, seconds later. Nothing that expects to keep an
address and reconnect to it — which is every NVR — can use one.

go2rtc solves the shape of the problem: it runs a command *per consumer
connection*, so the URL can be minted at the moment it is about to be used. This
project is that command, plus the configuration that wires it up.

The second thing go2rtc gives us matters just as much. It opens **one** upstream
connection per stream and fans it out to every consumer. Shinobi recording
around the clock keeps the stream warm, and Home Assistant attaching to the same
address costs nothing extra: no second cloud session, no second viewer slot on
the camera.

```
  MIPC cloud                    your NAS                     your network
 ┌───────────┐        ┌───────────────────────────┐        ┌──────────────┐
 │  camera   │───────▶│ mipc-restream  →  go2rtc  │───────▶│   Shinobi    │
 └───────────┘   one  │  mints a URL     serves   │  rtsp  │ Home Assistant│
                 pull │  per connect     :8554    │        │     VLC      │
                      └───────────────────────────┘        └──────────────┘
```

## Install on Synology

1. **Copy the folder to the NAS**, somewhere under a shared folder — for example
   `/volume1/docker/mipc-restream`.
2. **Create the `.env`**: copy `.env.example` to `.env` beside `compose.yaml` and
   fill in `MIPC_USERNAME` and `MIPC_PASSWORD`. These are the same credentials
   the MIPC phone app signs in with. **Write a `$` in the password as `$$`** —
   Compose substitutes variables in this file before the container sees it, so
   a lone `$` silently truncates the password and authentication fails with
   credentials that look right.
3. **Let the container write to `config/`.** It runs as uid 1000, and the bind
   mount hides the ownership the image sets, so the directory on the NAS has to
   be writable by that uid or the entrypoint cannot write `go2rtc.yaml`:

   ```sh
   chown 1000:1000 /volume1/docker/mipc-restream/config
   ```

4. **Container Manager → Project → Create.** Point it at the folder; it will find
   `compose.yaml` and build the image. The first build takes a few minutes,
   mostly ffmpeg.
5. **Check it came up** at `http://<nas>:1984/` — go2rtc's web UI lists every
   stream and will play one in the browser.

Each camera is then at `rtsp://<nas>:8554/<stream-name>`.

To see the names before you start, or afterwards:

```sh
docker exec mipc-restream mipc-restream discover
```

```
STREAM      SERIAL            STATUS   NAME
front_door  MIPC0000001       online   Front Door
back_gate   MIPC0000002       OFFLINE  Back Gate
```

## Point Shinobi at it

Add a monitor with:

| Field | Value |
|---|---|
| Input Type | `RTSP` (or `H.264 / H.265`) |
| Full URL | `rtsp://<nas>:8554/front_door` |
| RTSP Transport | `TCP` |

Nothing else is special. Shinobi may reconnect whenever it likes; each
reconnection quietly mints a new MIPC URL behind the scenes.

## Point Home Assistant at it

Add a **Generic Camera** integration with the same
`rtsp://<nas>:8554/front_door` as the stream source.

Keep the
[MIPC Camera v2](https://github.com/BusyChild77/hacs-mipc-camera-v2)
integration installed alongside it if you want snapshots and the online/offline
state — those go straight to MIPC and do not touch this container.

## Configuration

Everything is an environment variable, set in `.env`. See
[`.env.example`](.env.example) for the full list; the ones worth knowing about:

| Variable | Default | What it does |
|---|---|---|
| `MIPC_USERNAME`, `MIPC_PASSWORD` | — | The MIPC account. Required. |
| `MIPC_STREAM_PROFILE` | `p0` | Which encoding to pull. `p0` is the largest, `p1`–`p3` progressively smaller. |
| `MIPC_SERIALS` | *(all)* | Comma separated serials, to publish only some cameras. |
| `MIPC_READ_TIMEOUT` | `5` | Seconds before a silent upstream is given up on — and the delay before the first frame, since ffmpeg waits out the whole timeout during RTSP setup and go2rtc makes the viewer wait with it. Lower it if a client gives up before the stream appears; raise it on a flaky uplink. |
| `MIPC_FFMPEG_ARGS` | `-c copy -an` | What ffmpeg does with the video. The default copies it without re-encoding, and drops the audio: MIPC delivers its AAC track slowly enough to add ~7s to every connection, which is long enough for VLC to time out. Use `-c copy` to keep the sound. |

**About the profile.** The video crosses the internet once to reach the NAS and
is then served locally. On a domestic uplink, and with several cameras recording
around the clock, `p1` or `p2` is often the honest choice.

### Hand-written additions

`go2rtc.yaml` is regenerated at every start — that is how a camera renamed in
the MIPC app gets picked up. Anything you want to add by hand goes in
`config/go2rtc.overlay.yaml` instead, which is merged over the generated file,
key by key:

```yaml
# config/go2rtc.overlay.yaml
webrtc:
  candidates:
    - 192.168.1.10:8555   # the NAS, so browsers can reach the WebRTC path
streams:
  doorbell:
    - rtsp://192.168.1.50/stream1   # a camera that is not on the MIPC account
```

## What to expect

**Startup.** The container asks MIPC what cameras exist, writes `go2rtc.yaml`
and starts go2rtc. If MIPC is unreachable at that moment — an internet link that
comes up after the NAS does — the previous `go2rtc.yaml` is kept rather than the
recorder losing every monitor.

**Idle cost is zero.** Nothing is pulled from MIPC until something connects. A
camera nobody is watching costs nothing.

**Long connections are the unverified part.** MIPC's own web player never holds
a stream for days, so nothing is documented about what happens when Shinobi
does. If the stream drops, ffmpeg exits and go2rtc starts it again with a fresh
URL, which is the design — but expect an occasional gap in a 24/7 recording
until you have watched it for a while.

## Security notes

**The stream URL is a bearer token.** Anyone holding one can watch the camera
until it expires. So:

- Nothing here ever logs it. ffmpeg quotes URLs back in its own error messages,
  which is why this runs ffmpeg as a child process and filters its output rather
  than exec'ing it directly.
- It is passed to ffmpeg as an argument, so it is visible to `ps` **inside the
  container** for the life of the connection. That is acceptable because the
  container runs nothing else and its process namespace is its own. Do not add
  `pid: host` to the compose file.

**The RTSP server has no authentication** and is on your LAN. That is the usual
arrangement for an NVR, but it does mean anyone on the network can watch. Do not
forward 8554 out of the house.

**Credentials live in `.env`**, which is gitignored, and are never baked into the
image.

## Develop

```sh
python -m venv .venv && source .venv/bin/activate
pip install -e . -r requirements_dev.txt

pytest                              # coverage is on by default and stays at 100%
ruff check src tests
ruff format src tests
```

`src/mipc_client/` is a **generated copy** of the sibling
[`mipc-client`](../mipc-client) project — the Docker build context is this
folder and nothing above it, so the client cannot be installed from a sibling
path at build time. Do not edit it here. After changing the client:

```sh
./scripts/sync_client.sh            # refresh the copy
./scripts/sync_client.sh --check    # or just report drift
```

### Bumping go2rtc

The version is pinned in the [`Dockerfile`](Dockerfile) as `GO2RTC_VERSION`.
To verify the download, pass the checksum from the release page:

```sh
docker compose build --build-arg GO2RTC_VERSION=v1.9.9 --build-arg GO2RTC_SHA256=<sha>
```

### Architecture

Synology models are x86_64 or arm64; the Dockerfile reads the architecture off
the base image and picks the matching go2rtc build, so a plain `docker build`
works without buildx. Older 32-bit ARM units are handled too, but have never had
enough CPU to be worth it.

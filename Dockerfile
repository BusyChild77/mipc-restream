# One image, because go2rtc runs the URL-minting command as its own child
# process: splitting them into two containers would put `exec:` on the wrong
# side of the boundary.
#
# The base is Debian rather than Alpine on purpose. dukpy, which runs the
# JavaScript MIPC signs its requests with, publishes manylinux wheels only; on
# musl pip falls back to building it, and the image then needs a full toolchain.

ARG PYTHON_VERSION=3.13-slim

FROM python:${PYTHON_VERSION} AS builder

# Bump this to take a new go2rtc. Set GO2RTC_SHA256 to have the download
# verified; it is left empty by default so a build does not break every release.
ARG GO2RTC_VERSION=v1.9.9
ARG GO2RTC_SHA256=""

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# Container Manager builds without buildx, so TARGETARCH is not set for us and
# the architecture is read off the base image instead.
RUN set -eu; \
    case "$(dpkg --print-architecture)" in \
        amd64) arch=amd64 ;; \
        arm64) arch=arm64 ;; \
        armhf) arch=arm ;; \
        *) echo "no go2rtc build for $(dpkg --print-architecture)" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /go2rtc \
        "https://github.com/AlexxIT/go2rtc/releases/download/${GO2RTC_VERSION}/go2rtc_linux_${arch}"; \
    if [ -n "${GO2RTC_SHA256}" ]; then \
        echo "${GO2RTC_SHA256}  /go2rtc" | sha256sum -c -; \
    fi; \
    chmod +x /go2rtc

COPY pyproject.toml LICENSE README.md /build/
COPY src /build/src
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir /build


FROM python:${PYTHON_VERSION}

# ffmpeg is the only thing that touches the video. It never re-encodes by
# default; MIPC already handed us an encoded stream and a NAS should not spend
# its CPU encoding it again.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --create-home --uid 1000 restream \
    && mkdir -p /config \
    && chown restream:restream /config

COPY --from=builder /go2rtc /usr/local/bin/go2rtc
COPY --from=builder /opt/venv /opt/venv
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
# An absolute mode, not `chmod +x`: COPY preserves the mode the file had in the
# build context, and Container Manager uploads a folder with whatever the share
# gave it. `+x` on a 0600 upload yields 0701, and the entrypoint runs as
# `restream`, which then has execute but not read — fatal for a shell script.
RUN chmod 0755 /usr/local/bin/entrypoint.sh

ENV PATH="/opt/venv/bin:${PATH}" \
    MIPC_CONFIG=/config/go2rtc.yaml \
    MIPC_OVERLAY=/config/go2rtc.overlay.yaml \
    MIPC_CACHE=/config/devices.json \
    PYTHONUNBUFFERED=1

USER restream
WORKDIR /config
VOLUME ["/config"]

# 8554 RTSP (what Shinobi and Home Assistant read), 1984 the go2rtc web UI and
# API, 8555 WebRTC for the low latency path.
EXPOSE 8554/tcp 1984/tcp 8555/tcp 8555/udp

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]

#!/bin/sh
# Generate the go2rtc configuration from the account, then hand over to go2rtc.
#
# Generation is allowed to fail when the cause is MIPC being unreachable. An
# internet link that comes up after the NAS does is the normal case, and so is a
# device account whose camera happens to be away; neither must cost the recorder
# every monitor it has.
#
# What it must not do is reuse the file that is already there. That file was
# written by whichever build last reached MIPC, so keeping it is how a fix to
# the stream command never reaches the deployment that needs it. The camera
# cache is what avoids that: the listing is remembered, and the configuration is
# regenerated from it by the code running now.
#
# Settings that are simply wrong are a different matter, and stop the container
# rather than being papered over.
set -eu

CONFIG="${MIPC_CONFIG:-/config/go2rtc.yaml}"
OVERLAY="${MIPC_OVERLAY:-/config/go2rtc.overlay.yaml}"
CACHE="${MIPC_CACHE:-/config/devices.json}"

# How many times to ask MIPC what the account holds, and how long to leave
# between asking. A few spaced attempts ride out a link that comes up after the
# NAS without turning the container into a restart loop, which is what an
# immediate exit becomes under `restart: unless-stopped`.
ATTEMPTS="${MIPC_CONFIG_ATTEMPTS:-3}"
DELAY="${MIPC_CONFIG_RETRY_DELAY:-10}"

# The status is captured rather than tested with `if !`, which would replace it
# with the negation and lose the distinction this whole block is about.
attempt=1
status=0
while : ; do
    status=0
    mipc-restream config \
        --output "$CONFIG" --overlay "$OVERLAY" --cache "$CACHE" || status=$?

    # `if` rather than `[ ... ] && break`: under `set -eu` a test that fails is
    # the last command of the body, and the shell exits on it.
    if [ "$status" -eq 0 ] || [ "$status" -eq 2 ] || [ "$attempt" -ge "$ATTEMPTS" ]
    then
        break
    fi

    echo "WARNING mipc-restream: could not reach MIPC (attempt $attempt of $ATTEMPTS);" >&2
    echo "        trying again in ${DELAY}s" >&2
    sleep "$DELAY"
    attempt=$((attempt + 1))
done

if [ "$status" -ne 0 ]; then
    if [ "$status" -eq 2 ]; then
        echo "ERROR mipc-restream: fix the settings in .env, or the ownership of" >&2
        echo "      the config volume (it must be writable by uid 1000), then restart" >&2
        exit 2
    fi

    # Reached only when MIPC has never answered on this deployment: with a cache
    # the configuration is generated without it, and the exit above never fires.
    if [ -f "$CONFIG" ]; then
        echo "WARNING mipc-restream: could not reach MIPC and have never seen this" >&2
        echo "        account; keeping $CONFIG, which an older build may have written" >&2
    else
        echo "ERROR mipc-restream: could not reach MIPC and there is no $CONFIG" >&2
        exit 1
    fi
fi

exec go2rtc -config "$CONFIG"

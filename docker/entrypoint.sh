#!/bin/sh
# Generate the go2rtc configuration from the account, then hand over to go2rtc.
#
# Generation is allowed to fail when the cause is MIPC being unreachable. An
# internet link that comes up after the NAS does is the normal case, and it must
# not cost the recorder every monitor it has: the previous configuration is good
# enough to start with, and go2rtc picks the cameras up as they answer.
#
# Settings that are simply wrong are a different matter, and stop the container
# rather than being papered over with a stale file.
set -eu

CONFIG="${MIPC_CONFIG:-/config/go2rtc.yaml}"
OVERLAY="${MIPC_OVERLAY:-/config/go2rtc.overlay.yaml}"

# The status is captured rather than tested with `if !`, which would replace it
# with the negation and lose the distinction this whole block is about.
status=0
mipc-restream config --output "$CONFIG" --overlay "$OVERLAY" || status=$?

if [ "$status" -ne 0 ]; then
    if [ "$status" -eq 2 ]; then
        echo "ERROR mipc-restream: fix the settings in .env, or the ownership of" >&2
        echo "      the config volume (it must be writable by uid 1000), then restart" >&2
        exit 2
    fi

    if [ -f "$CONFIG" ]; then
        echo "WARNING mipc-restream: could not reach MIPC; keeping $CONFIG" >&2
    else
        echo "ERROR mipc-restream: could not reach MIPC and there is no $CONFIG" >&2
        exit 1
    fi
fi

exec go2rtc -config "$CONFIG"

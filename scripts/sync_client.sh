#!/bin/sh
# Refresh the vendored copy of mipc-client from the sibling checkout.
#
# The copy is committed because the Docker build context is this folder and
# nothing above it: Container Manager uploads one directory, and a build cannot
# reach out to a sibling repository. Run this after changing the client.
set -eu

here=$(cd "$(dirname "$0")/.." && pwd)
client=${MIPC_CLIENT_DIR:-$(cd "$here/../mipc-client" && pwd)}

exec python3 "$client/tools/vendor.py" "$here/src/mipc_client" "$@"

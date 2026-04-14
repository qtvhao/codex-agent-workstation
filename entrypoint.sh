#!/bin/bash
set -e

# Wait for the novnc X socket to become available
SOCKET=/tmp/.X11-unix/X0
TIMEOUT=30
elapsed=0
while [ ! -S "$SOCKET" ]; do
    if [ "$elapsed" -ge "$TIMEOUT" ]; then
        echo "ERROR: Timed out waiting for X socket at $SOCKET"
        exit 1
    fi
    echo "Waiting for X socket ($elapsed/$TIMEOUT)s..."
    sleep 1
    elapsed=$((elapsed + 1))
done

echo "X socket available at $SOCKET"
exec "$@"

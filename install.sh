#!/bin/sh
# Compatibility entry point for the shared Python installer.
exec python3 "$(dirname -- "$0")/install.py" "$@"

#!/usr/bin/env bash
# Publish a Keystone Grid Radio episode to the private feed.
# Usage: publish.sh <mp3> | publish.sh --backfill | publish.sh --dry-run
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$DIR/publish.py" "$@"

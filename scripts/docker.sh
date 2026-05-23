#!/usr/bin/env sh
set -eu

if [ "$#" -eq 0 ]; then
  set -- --help
fi

# Ad-hoc rentmap subcommand against the lightweight image. The long-running
# `rentmap-server` container already runs the hourly scheduler; this helper
# is for one-off manual runs (e.g. immediate crawl, custom date, regenerating
# web from existing CSVs).
docker compose run --rm rentmap python scripts/rentmap.py "$@"

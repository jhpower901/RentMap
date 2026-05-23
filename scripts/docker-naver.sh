#!/usr/bin/env sh
set -eu

if [ "$#" -eq 0 ]; then
  set -- crawl-naver
fi

# Ad-hoc naver-side rentmap subcommand. The long-running `rentmap-naver`
# container runs the every-3h scheduler; this helper is for manual runs.
docker compose run --rm rentmap-naver python scripts/rentmap.py "$@"

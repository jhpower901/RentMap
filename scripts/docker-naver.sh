#!/usr/bin/env sh
set -eu

if [ "$#" -eq 0 ]; then
  set -- crawl-naver
fi

docker compose --profile naver run --rm rentmap-naver "$@"

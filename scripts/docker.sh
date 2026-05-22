#!/usr/bin/env sh
set -eu

if [ "$#" -eq 0 ]; then
  set -- --help
fi

docker compose run --rm rentmap "$@"

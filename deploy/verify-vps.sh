#!/bin/sh
set -eu

PROJECT_DIR="${HXYLIVE_PROJECT_DIR:-/opt/hxylive}"

test -f "$PROJECT_DIR/docker-compose.yml"
test -f "$PROJECT_DIR/.env"
test -d "$PROJECT_DIR/data"
docker inspect hxylive >/dev/null
docker inspect flaresolverr >/dev/null
nginx -t
curl --fail --silent --show-error --max-time 10 http://127.0.0.1:8080/api/version
printf '\n%s\n' 'HXYLIVE VPS verification passed.'

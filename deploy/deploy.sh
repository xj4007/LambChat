#!/bin/bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

mkdir -p workspace uploads
docker compose build lambchat
docker compose run --rm --no-deps --user root --entrypoint sh lambchat \
  -c 'chown -R app:app /app/data /app/workspace /app/uploads'
docker compose up -d --no-build
docker compose ps

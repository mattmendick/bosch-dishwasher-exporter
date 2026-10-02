#!/usr/bin/env bash
# Update and redeploy without removing the persistent auth-token volume.
set -euo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd -- "$PROJECT_DIR"

git pull --ff-only
docker compose build --pull
docker compose up -d --no-build
docker compose ps

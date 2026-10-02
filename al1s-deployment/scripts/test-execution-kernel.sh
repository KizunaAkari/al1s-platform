#!/usr/bin/env sh
set -eu

deployment_root="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
workspace_root="$(dirname "$deployment_root")"
compose_file="$deployment_root/compose/compose.yaml"
env_file="$deployment_root/.env"

if [ ! -f "$env_file" ]; then
  echo "Missing deployment environment file: $env_file" >&2
  exit 1
fi

compose() {
  docker compose --env-file "$env_file" -f "$compose_file" "$@"
}

cleanup() {
  compose exec -T postgres sh -ec \
    'dropdb --if-exists -U "$POSTGRES_USER" al1s_execution_test'
}

cd "$workspace_root"
docker info >/dev/null
compose up -d --wait postgres seaweed-s3 s3-init
compose exec -T postgres sh -ec \
  'dropdb --if-exists -U "$POSTGRES_USER" al1s_execution_test && createdb -U "$POSTGRES_USER" al1s_execution_test'

trap cleanup EXIT INT TERM
compose --profile test build execution-test
compose --profile test run --rm execution-test

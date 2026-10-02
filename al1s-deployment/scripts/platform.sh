#!/usr/bin/env sh
set -eu

command_name="${1:-start}"
deployment_root="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
workspace_root="$(dirname "$deployment_root")"
compose_file="$deployment_root/compose/compose.unified.yaml"
tls_compose_file="$deployment_root/compose/compose.tls.yaml"
env_file="$deployment_root/.env"
tls_env_file="$deployment_root/.env.tls"

if [ ! -f "$env_file" ]; then
  cp "$deployment_root/.env.example" "$env_file"
  echo "Created .env from local-development defaults. Change credentials before non-local use." >&2
fi

compose() {
  docker compose --env-file "$env_file" -f "$compose_file" "$@"
}

tls_compose() {
  docker compose --env-file "$env_file" --env-file "$tls_env_file" -f "$compose_file" -f "$tls_compose_file" "$@"
}

initialize_tls_env() {
  if [ ! -f "$tls_env_file" ]; then
    cp "$deployment_root/.env.tls.example" "$tls_env_file"
    echo "Created .env.tls. Set its public DNS name/ports to match the generated server certificate." >&2
  fi
}

assert_tls_assets() {
  for name in ca.crt server.crt server.key; do
    if [ ! -f "$deployment_root/compose/tls/generated/$name" ]; then
      echo "Missing TLS asset: $deployment_root/compose/tls/generated/$name" >&2
      exit 1
    fi
  done
}

docker info >/dev/null
cd "$workspace_root"

case "$command_name" in
  start)
    compose up -d --build --wait
    compose ps
    echo "AL-1S Next: http://localhost:8180"
    ;;
  start-tls)
    initialize_tls_env
    assert_tls_assets
    tls_compose up -d --build --wait
    tls_compose ps
    echo "AL-1S Next TLS: https://localhost:8443"
    ;;
  stop) compose down ;;
  stop-tls)
    initialize_tls_env
    tls_compose down
    ;;
  restart)
    compose down
    compose up -d --build --wait
    ;;
  restart-tls)
    initialize_tls_env
    assert_tls_assets
    tls_compose down
    tls_compose up -d --build --wait
    ;;
  status) compose ps ;;
  logs) compose logs --tail 200 -f ;;
  config) compose config ;;
  config-tls)
    initialize_tls_env
    tls_compose config
    ;;
  test)
    docker build --target test -t al1s-next-backend-test "$workspace_root/backend/al1s-backend"
    docker run --rm al1s-next-backend-test
    docker build --target test -t al1s-next-frontend-test "$workspace_root/al1s-frontend"
    docker run --rm al1s-next-frontend-test
    ;;
  kernel-test) "$deployment_root/scripts/test-platform-kernel.sh" ;;
  *)
    echo "Usage: $0 {start|start-tls|stop|stop-tls|restart|restart-tls|status|logs|config|config-tls|test|kernel-test}" >&2
    exit 2
    ;;
esac

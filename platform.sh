#!/usr/bin/env bash

set -Eeuo pipefail

command_name="${1:-start}"
if (($#)); then shift; fi
package_path=""
if [[ "${1:-}" && "${1:-}" != --* ]]; then
  package_path="$1"
  shift
fi

no_image=false
keep_local_env=false
no_browser=false
assume_yes=false
while (($#)); do
  case "$1" in
    --no-image) no_image=true ;;
    --keep-local-env) keep_local_env=true ;;
    --no-browser) no_browser=true ;;
    --yes) assume_yes=true ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

case "$command_name" in
  start|stop|status|backup|restore|export|import|verify|logs) ;;
  *) echo "Usage: bash platform.sh {start|stop|status|backup|restore|export|import|verify|logs} [package] [options]" >&2; exit 2 ;;
esac

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
backups_root="$project_root/backups"
scripts_root="$project_root/scripts"
volume_name="maa-platform-data"
image_name="maa-test-platform:demo"
service_name="control-center"
container_name="maa-test-platform"
platform_url="http://127.0.0.1:8000"
package_format=1
platform_version="$(tr -d '\r\n' < "$project_root/VERSION")"
active_staging=""

absolute_path() {
  if [[ "$1" = /* ]]; then
    printf '%s\n' "$1"
  else
    printf '%s\n' "$project_root/$1"
  fi
}

assert_package_dependencies() {
  command -v tar >/dev/null || { echo "tar is required" >&2; exit 1; }
  command -v sha256sum >/dev/null || { echo "sha256sum is required" >&2; exit 1; }
}

assert_docker() {
  command -v docker >/dev/null || { echo "docker is required" >&2; exit 1; }
  docker info >/dev/null
}

new_staging_directory() {
  mkdir -p "$backups_root"
  mktemp -d "$backups_root/.stage-XXXXXX"
}

remove_staging_directory() {
  local target="$1"
  case "$target" in
    "$backups_root"/.stage-*) rm -rf -- "$target" ;;
    *) echo "Refusing to remove staging directory outside backups: $target" >&2; return 1 ;;
  esac
}

cleanup_on_exit() {
  if [[ -n "$active_staging" && -d "$active_staging" ]]; then
    remove_staging_directory "$active_staging"
  fi
}
trap cleanup_on_exit EXIT

image_exists() {
  docker image inspect "$image_name" >/dev/null 2>&1
}

ensure_image() {
  image_exists || docker compose build "$service_name"
}

volume_exists() {
  docker volume inspect "$volume_name" >/dev/null 2>&1
}

ensure_volume() {
  volume_exists || docker volume create "$volume_name" >/dev/null
}

service_running() {
  docker compose ps --status running --services 2>/dev/null | grep -Fxq "$service_name"
}

wait_platform_health() {
  local status=""
  for _attempt in $(seq 1 45); do
    status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container_name" 2>/dev/null || true)"
    if [[ "$status" == healthy ]]; then
      echo "Platform is healthy: $platform_url (v$platform_version)"
      return 0
    fi
    sleep 1
  done
  docker compose logs --tail 80 "$service_name" || true
  echo "Platform did not become healthy in time." >&2
  return 1
}

start_platform() {
  local build_flag="--build"
  [[ "${1:-}" == no-build ]] && build_flag="--no-build"
  docker compose up -d "$build_flag"
  wait_platform_health
  if [[ "$no_browser" == false && -n "${DISPLAY:-}" ]] && command -v xdg-open >/dev/null; then
    xdg-open "$platform_url" >/dev/null 2>&1 &
  fi
}

invoke_volume_tool() {
  local operation="$1"
  local archive_path="$2"
  local archive_directory archive_name volume_mount package_mount
  ensure_image
  archive_directory="$(dirname "$archive_path")"
  archive_name="$(basename "$archive_path")"
  if [[ "$operation" == backup ]]; then
    volume_mount="$volume_name:/volume:ro"
    package_mount="$archive_directory:/package"
  else
    volume_mount="$volume_name:/volume"
    package_mount="$archive_directory:/package:ro"
  fi
  docker run --rm --entrypoint python \
    -v "$volume_mount" \
    -v "$scripts_root:/tools:ro" \
    -v "$package_mount" \
    "$image_name" \
    /tools/volume_archive.py "$operation" \
    --root /volume \
    --archive "/package/$archive_name"
}

new_data_archive() {
  local archive_path="$1"
  local allow_new_volume="${2:-false}"
  local was_running=false
  if ! volume_exists; then
    if [[ "$allow_new_volume" != true ]]; then
      echo "Docker volume '$volume_name' does not exist. Start the platform before creating a backup." >&2
      return 1
    fi
    ensure_volume
  fi
  service_running && was_running=true
  if [[ "$was_running" == true ]]; then docker compose stop "$service_name"; fi
  if ! invoke_volume_tool backup "$archive_path"; then
    if [[ "$was_running" == true ]]; then docker compose start "$service_name"; fi
    return 1
  fi
  if [[ "$was_running" == true ]]; then
    docker compose start "$service_name"
    wait_platform_health
  fi
}

restore_data_archive() {
  local archive_path="$1"
  local timestamp safety_archive restore_failed=false
  ensure_volume
  ensure_image
  mkdir -p "$backups_root"
  timestamp="$(date +%Y%m%d-%H%M%S)"
  safety_archive="$backups_root/pre-restore-$timestamp-platform-data.tar.gz"
  if service_running; then docker compose stop "$service_name"; fi
  invoke_volume_tool backup "$safety_archive"
  if ! invoke_volume_tool restore "$archive_path"; then
    restore_failed=true
    echo "Restore failed; rolling back from $safety_archive" >&2
    invoke_volume_tool restore "$safety_archive"
  fi
  docker compose up -d --no-build
  wait_platform_health
  echo "Pre-restore safety backup: $safety_archive"
  [[ "$restore_failed" == false ]]
}

write_checksums() {
  local directory="$1"
  local files=(VERSION manifest.json platform-data.tar.gz)
  [[ -f "$directory/platform.env" ]] && files+=(platform.env)
  [[ -f "$directory/platform-image.tar" ]] && files+=(platform-image.tar)
  (cd "$directory" && sha256sum "${files[@]}" > checksums.sha256)
}

validate_package_entries() {
  local archive="$1" entry normalized
  while IFS= read -r entry; do
    normalized="${entry#./}"
    if [[ "$normalized" == /* || "$normalized" == ../* || "$normalized" == *"/../"* || "$normalized" == */.. ]]; then
      echo "Package contains an unsafe path: $entry" >&2
      return 1
    fi
  done < <(tar -tzf "$archive")
}

test_checksums() {
  local directory="$1"
  [[ -f "$directory/checksums.sha256" ]] || { echo "Package is missing checksums.sha256" >&2; return 1; }
  (cd "$directory" && sha256sum -c checksums.sha256 >&2)
}

new_platform_package() {
  local output_path="$1"
  local include_image="$2"
  local staging has_environment=false image_json="null" absolute_output
  staging="$(new_staging_directory)"
  active_staging="$staging"
  new_data_archive "$staging/platform-data.tar.gz"
  if [[ -f "$project_root/.env" ]]; then
    cp "$project_root/.env" "$staging/platform.env"
    has_environment=true
  fi
  cp "$project_root/VERSION" "$staging/VERSION"
  if [[ "$include_image" == true ]]; then
    ensure_image
    docker save --output "$staging/platform-image.tar" "$image_name"
    image_json="\"$image_name\""
  fi
  cat > "$staging/manifest.json" <<EOF
{
  "package_format": $package_format,
  "platform": "maa-test-platform",
  "version": "$platform_version",
  "created_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "volume": "$volume_name",
  "image": $image_json,
  "environment_included": $has_environment
}
EOF
  write_checksums "$staging"
  absolute_output="$(absolute_path "$output_path")"
  mkdir -p "$(dirname "$absolute_output")"
  [[ ! -e "$absolute_output" ]] || { echo "Output package already exists: $absolute_output" >&2; return 1; }
  tar -czf "$absolute_output" -C "$staging" .
  chmod 600 "$absolute_output" 2>/dev/null || true
  echo "Platform package created: $absolute_output"
  echo "WARNING: The package contains the platform database and notification encryption key; store it as a secret." >&2
  if [[ "$has_environment" == true ]]; then
    echo "WARNING: The package also contains platform.env." >&2
  fi
  remove_staging_directory "$staging"
  active_staging=""
}

expand_platform_package() {
  local input_path="$1"
  local absolute_input staging required package_version
  absolute_input="$(absolute_path "$input_path")"
  [[ -f "$absolute_input" ]] || { echo "Package does not exist: $absolute_input" >&2; return 1; }
  validate_package_entries "$absolute_input"
  staging="$(new_staging_directory)"
  if ! tar -xzf "$absolute_input" -C "$staging"; then
    remove_staging_directory "$staging"
    return 1
  fi
  for required in platform-data.tar.gz VERSION manifest.json checksums.sha256; do
    if [[ ! -f "$staging/$required" ]]; then
      echo "Package is missing $required" >&2
      remove_staging_directory "$staging"
      return 1
    fi
  done
  if ! test_checksums "$staging"; then
    remove_staging_directory "$staging"
    return 1
  fi
  if ! grep -Eq '"package_format"[[:space:]]*:[[:space:]]*1([,[:space:]]|$)' "$staging/manifest.json" ||
     ! grep -Eq '"platform"[[:space:]]*:[[:space:]]*"maa-test-platform"' "$staging/manifest.json"; then
    echo "Unsupported platform package format." >&2
    remove_staging_directory "$staging"
    return 1
  fi
  package_version="$(tr -d '\r\n' < "$staging/VERSION")"
  if [[ "$package_version" != "$platform_version" ]]; then
    echo "WARNING: Package version is $package_version; local platform source is $platform_version." >&2
  fi
  printf '%s\n' "$staging"
}

is_platform_package() {
  local archive="$1"
  local entries
  entries="$(tar -tzf "$archive" 2>/dev/null | sed 's#^\./##')" || return 1
  grep -Fxq manifest.json <<<"$entries" && grep -Fxq platform-data.tar.gz <<<"$entries"
}

confirm_restore() {
  [[ "$assume_yes" == true ]] && return
  local answer
  read -r -p "This will replace the current platform data volume. Type RESTORE to continue: " answer
  [[ "$answer" == RESTORE ]] || { echo "Restore cancelled." >&2; return 1; }
}

import_environment() {
  local staging="$1"
  local saved
  [[ -f "$staging/platform.env" ]] || return 0
  if [[ "$keep_local_env" == true ]]; then
    echo "Keeping the current .env file."
    return 0
  fi
  if [[ -f "$project_root/.env" ]]; then
    mkdir -p "$backups_root"
    saved="$backups_root/pre-import-$(date +%Y%m%d-%H%M%S).env"
    cp "$project_root/.env" "$saved"
    echo "Existing .env saved to: $saved"
  fi
  cp "$staging/platform.env" "$project_root/.env"
  chmod 600 "$project_root/.env" 2>/dev/null || true
}

restore_platform_package() {
  local input_path="$1"
  local load_image="$2"
  local staging
  confirm_restore
  staging="$(expand_platform_package "$input_path")"
  active_staging="$staging"
  import_environment "$staging"
  if [[ "$load_image" == true && -f "$staging/platform-image.tar" ]]; then
    docker load --input "$staging/platform-image.tar"
  elif ! image_exists; then
    docker compose build "$service_name"
  fi
  restore_data_archive "$staging/platform-data.tar.gz"
  remove_staging_directory "$staging"
  active_staging=""
}

cd "$project_root"
assert_package_dependencies

if [[ "$command_name" == verify ]]; then
  [[ -n "$package_path" ]] || { echo "verify requires a package path" >&2; exit 2; }
  verified_staging="$(expand_platform_package "$package_path")"
  active_staging="$verified_staging"
  cat "$verified_staging/manifest.json"
  echo "Package verification succeeded: $(absolute_path "$package_path")"
  remove_staging_directory "$verified_staging"
  active_staging=""
  exit 0
fi

assert_docker

case "$command_name" in
  start)
    start_platform
    ;;
  stop)
    docker compose down
    ;;
  status)
    docker compose ps
    docker inspect --format '{{json .State.Health}}' "$container_name" 2>/dev/null || true
    ;;
  logs)
    docker compose logs --tail 200 "$service_name"
    ;;
  backup)
    package_path="${package_path:-backups/maa-platform-backup-$(date +%Y%m%d-%H%M%S).tar.gz}"
    new_platform_package "$package_path" false
    ;;
  export)
    package_path="${package_path:-backups/maa-platform-export-$(date +%Y%m%d-%H%M%S).tar.gz}"
    include_image=true
    [[ "$no_image" == true ]] && include_image=false
    new_platform_package "$package_path" "$include_image"
    ;;
  restore)
    [[ -n "$package_path" ]] || { echo "restore requires a package path" >&2; exit 2; }
    restore_path="$(absolute_path "$package_path")"
    [[ -f "$restore_path" ]] || { echo "Backup does not exist: $restore_path" >&2; exit 1; }
    if is_platform_package "$restore_path"; then
      restore_platform_package "$restore_path" false
    else
      confirm_restore
      restore_data_archive "$restore_path"
    fi
    ;;
  import)
    [[ -n "$package_path" ]] || { echo "import requires a package path" >&2; exit 2; }
    restore_platform_package "$package_path" true
    ;;
esac

#!/usr/bin/env bash
#
# Project: nextcloud-task-google-calendar-sync
# Script: update_stack.sh
# Version: 0.2.0
#
# Synopsis:
#   ./update_stack.sh [--yes] [--no-build] [--no-up] [--no-cache] [--upgrade-package PACKAGE] [--project-dir DIR] [--python-image IMAGE]
#
# Description:
#   Controlled update helper for the Nextcloud task to Google Calendar sync stack.
#   The script backs up the currently working requirements.txt directly under ./backups
#   using a YYMMDD_ filename prefix, updates pinned Python dependencies via pip-tools
#   inside a Docker Python image, shows a diff, optionally rebuilds the Docker image,
#   and restarts the Docker Compose stack.
#
# Requirements:
#   - bash
#   - docker with docker compose plugin
#   - requirements.in
#   - requirements.txt
#   - docker-compose.yml or compose.yml
#
# Notes:
#   - Script output is intentionally English.
#   - requirements.txt is backed up before any modification.
#   - Backup file format: ./backups/YYMMDD_requirements.txt or ./backups/YYMMDD_HHMMSS_requirements.txt if needed.
#   - By default, all Python dependencies from requirements.in are upgraded.
#   - Use --upgrade-package PACKAGE to upgrade only a single package.
#   - pip-compile runs inside Docker to avoid host Python version mismatches.
#

set -Eeuo pipefail

SCRIPT_VERSION="0.2.0"
PROJECT_DIR="$(pwd)"
BACKUP_DIR=""
BACKUP_FILE=""
ASSUME_YES="false"
DO_BUILD="true"
DO_UP="true"
NO_CACHE="false"
UPGRADE_PACKAGE=""
PYTHON_IMAGE=""

log() {
  printf '[INFO] %s\n' "$*"
}

warn() {
  printf '[WARN] %s\n' "$*" >&2
}

err() {
  printf '[ERROR] %s\n' "$*" >&2
}

usage() {
  cat <<'EOF'
update_stack.sh - Controlled dependency and Docker stack updater

Usage:
  ./update_stack.sh [OPTIONS]

Options:
  --yes, -y                    Run without interactive confirmations.
  --no-build                   Do not run docker compose build.
  --no-up                      Do not run docker compose up -d.
  --no-cache                   Build Docker image without cache.
  --upgrade-package PACKAGE    Upgrade only the given Python package via pip-compile.
  --project-dir DIR            Project directory. Defaults to current directory.
  --python-image IMAGE         Python image used for pip-compile. Defaults to the first FROM image in Dockerfile.
  --help, -h                   Show this help.

Examples:
  ./update_stack.sh
  ./update_stack.sh --yes
  ./update_stack.sh --upgrade-package requests
  ./update_stack.sh --no-cache
  ./update_stack.sh --python-image python:3.12-slim
  ./update_stack.sh --no-build --no-up

Recommended workflow:
  1. Set DRY_RUN=true in .env.
  2. Run this script.
  3. Check container logs.
  4. Set DRY_RUN=false after validation.
EOF
}

confirm() {
  local prompt="$1"
  if [[ "$ASSUME_YES" == "true" ]]; then
    return 0
  fi

  local answer
  read -r -p "$prompt [y/N]: " answer
  case "$answer" in
    y|Y|yes|YES) return 0 ;;
    *) return 1 ;;
  esac
}

cleanup_on_error() {
  local exit_code=$?
  err "Update failed with exit code ${exit_code}."

  if [[ -n "${BACKUP_FILE:-}" && -f "${BACKUP_FILE}" && -f "${PROJECT_DIR}/requirements.txt" ]]; then
    warn "A backup exists at: ${BACKUP_FILE}"
    warn "To restore manually, run:"
    warn "  cp '${BACKUP_FILE}' '${PROJECT_DIR}/requirements.txt'"
  fi

  exit "$exit_code"
}

detect_python_image_from_dockerfile() {
  local dockerfile_path="${PROJECT_DIR}/Dockerfile"

  if [[ ! -f "$dockerfile_path" ]]; then
    return 1
  fi

  # Extract first non-comment FROM image.
  awk '
    BEGIN { IGNORECASE=1 }
    /^[[:space:]]*#/ { next }
    /^[[:space:]]*FROM[[:space:]]+/ {
      print $2
      exit
    }
  ' "$dockerfile_path"
}

run_pip_compile_in_docker() {
  local compile_command

  if [[ -n "$UPGRADE_PACKAGE" ]]; then
    compile_command="python -m pip install --upgrade pip pip-tools >/dev/null && pip-compile --upgrade-package '${UPGRADE_PACKAGE}' --output-file requirements.txt requirements.in"
  else
    compile_command="python -m pip install --upgrade pip pip-tools >/dev/null && pip-compile --upgrade --output-file requirements.txt requirements.in"
  fi

  log "Running pip-compile inside Docker image: ${PYTHON_IMAGE}"

  docker run --rm \
    --pull=always \
    -v "${PROJECT_DIR}:/app" \
    -w /app \
    "${PYTHON_IMAGE}" \
    sh -c "$compile_command"
}

trap cleanup_on_error ERR

while [[ $# -gt 0 ]]; do
  case "$1" in
    --yes|-y)
      ASSUME_YES="true"
      shift
      ;;
    --no-build)
      DO_BUILD="false"
      shift
      ;;
    --no-up)
      DO_UP="false"
      shift
      ;;
    --no-cache)
      NO_CACHE="true"
      shift
      ;;
    --upgrade-package)
      if [[ $# -lt 2 ]]; then
        err "--upgrade-package requires a package name."
        exit 2
      fi
      UPGRADE_PACKAGE="$2"
      shift 2
      ;;
    --project-dir)
      if [[ $# -lt 2 ]]; then
        err "--project-dir requires a directory."
        exit 2
      fi
      PROJECT_DIR="$2"
      shift 2
      ;;
    --python-image)
      if [[ $# -lt 2 ]]; then
        err "--python-image requires an image name."
        exit 2
      fi
      PYTHON_IMAGE="$2"
      shift 2
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      err "Unknown argument: $1"
      usage
      exit 2
      ;;
  esac
done

PROJECT_DIR="$(cd "$PROJECT_DIR" && pwd)"
cd "$PROJECT_DIR"

log "nextcloud-task-google-calendar-sync updater version ${SCRIPT_VERSION}"
log "Project directory: ${PROJECT_DIR}"

if [[ ! -f "requirements.in" ]]; then
  err "requirements.in not found in ${PROJECT_DIR}."
  exit 1
fi

if [[ ! -f "requirements.txt" ]]; then
  err "requirements.txt not found in ${PROJECT_DIR}."
  exit 1
fi

if [[ ! -f "Dockerfile" ]]; then
  err "Dockerfile not found in ${PROJECT_DIR}."
  exit 1
fi

if [[ ! -f "docker-compose.yml" && ! -f "compose.yml" && ! -f "compose.yaml" ]]; then
  err "No docker-compose.yml, compose.yml, or compose.yaml found in ${PROJECT_DIR}."
  exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
  err "docker is not installed or not in PATH."
  exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
  err "docker compose plugin is not available."
  exit 1
fi

if [[ -z "$PYTHON_IMAGE" ]]; then
  PYTHON_IMAGE="$(detect_python_image_from_dockerfile || true)"
fi

if [[ -z "$PYTHON_IMAGE" ]]; then
  err "Could not detect Python image from Dockerfile. Use --python-image python:3.12-slim."
  exit 1
fi

case "$PYTHON_IMAGE" in
  python:*)
    ;;
  *)
    warn "Detected base image is not an official python image: ${PYTHON_IMAGE}"
    warn "pip-compile may fail if this image does not include Python and pip."
    if ! confirm "Continue with this image?"; then
      log "Aborted by user."
      exit 0
    fi
    ;;
esac

log "Dependency compile image: ${PYTHON_IMAGE}"

if [[ -f ".env" ]]; then
  if grep -Eq '^[[:space:]]*DRY_RUN[[:space:]]*=[[:space:]]*false[[:space:]]*$' .env; then
    warn "DRY_RUN=false detected in .env."
    warn "For dependency updates, consider setting DRY_RUN=true before running the updated container."
    if ! confirm "Continue although DRY_RUN=false is configured?"; then
      log "Aborted by user."
      exit 0
    fi
  fi
else
  warn ".env not found. Continuing, but the container may fail to start if required variables are missing."
fi

backup_date_prefix="$(date +%y%m%d)"
backup_timestamp="$(date +%y%m%d_%H%M%S)"
BACKUP_DIR="${PROJECT_DIR}/backups"
BACKUP_FILE="${BACKUP_DIR}/${backup_date_prefix}_requirements.txt"

mkdir -p "$BACKUP_DIR"

if [[ -e "$BACKUP_FILE" ]]; then
  BACKUP_FILE="${BACKUP_DIR}/${backup_timestamp}_requirements.txt"
fi

cp -a "requirements.txt" "$BACKUP_FILE"
log "Backed up requirements.txt to ${BACKUP_FILE}"

if [[ -d ".git" ]]; then
  if ! git diff --quiet -- requirements.txt requirements.in Dockerfile docker-compose.yml compose.yml compose.yaml 2>/dev/null; then
    warn "There are uncommitted changes in dependency or container files."
    git status --short -- requirements.txt requirements.in Dockerfile docker-compose.yml compose.yml compose.yaml || true
    if ! confirm "Continue with uncommitted changes?"; then
      log "Aborted by user."
      exit 0
    fi
  fi
fi

if [[ -n "$UPGRADE_PACKAGE" ]]; then
  log "Updating pinned Python dependency for package only: ${UPGRADE_PACKAGE}"
else
  log "Updating all pinned Python dependencies from requirements.in"
fi

run_pip_compile_in_docker

if cmp -s "$BACKUP_FILE" "requirements.txt"; then
  log "No changes in requirements.txt."
else
  log "requirements.txt changed. Diff against backup:"
  diff -u "$BACKUP_FILE" "requirements.txt" || true

  if ! confirm "Keep the updated requirements.txt?"; then
    cp -a "$BACKUP_FILE" "requirements.txt"
    log "Restored original requirements.txt from backup."
    exit 0
  fi
fi

if grep -q "with Python" requirements.txt; then
  log "pip-compile header:"
  grep -m 1 "with Python" requirements.txt || true
fi

if [[ "$DO_BUILD" == "true" ]]; then
  build_args=(build --pull)
  if [[ "$NO_CACHE" == "true" ]]; then
    build_args+=(--no-cache)
  fi

  log "Running: docker compose ${build_args[*]}"
  docker compose "${build_args[@]}"
else
  log "Skipping docker compose build."
fi

if [[ "$DO_UP" == "true" ]]; then
  if confirm "Start/recreate the stack with docker compose up -d?"; then
    log "Running: docker compose up -d"
    docker compose up -d
  else
    log "Skipping docker compose up -d."
  fi
else
  log "Skipping docker compose up -d."
fi

log "Current compose status:"
docker compose ps || true

log "Update completed successfully."
log "Backup location: ${BACKUP_FILE}"
log "Recommended next check:"
log "  docker compose logs -f"

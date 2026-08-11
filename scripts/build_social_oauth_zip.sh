#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUTPUT_PATH="${1:-${ROOT_DIR}/dist/cs2-social-oauth.zip}"
BUILD_DIR="$(mktemp -d)"

cleanup() {
  rm -rf "${BUILD_DIR}"
}
trap cleanup EXIT

mkdir -p "${BUILD_DIR}/cs2bot" "$(dirname "${OUTPUT_PATH}")"
cp "${ROOT_DIR}/cs2bot/__init__.py" "${BUILD_DIR}/cs2bot/__init__.py"
cp "${ROOT_DIR}/cs2bot/social_oauth.py" "${BUILD_DIR}/cs2bot/social_oauth.py"
cp "${ROOT_DIR}/requirements-social-oauth.txt" "${BUILD_DIR}/requirements.txt"

(
  cd "${BUILD_DIR}"
  zip -qr "${OUTPUT_PATH}" cs2bot requirements.txt
)

echo "Built ${OUTPUT_PATH}"

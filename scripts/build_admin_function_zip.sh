#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUTPUT_PATH="${1:-${ROOT_DIR}/dist/telegram-admin-function.zip}"
BUILD_DIR="$(mktemp -d)"
trap 'rm -rf "${BUILD_DIR}"' EXIT
mkdir -p "${BUILD_DIR}" "$(dirname "${OUTPUT_PATH}")"
cp -R "${ROOT_DIR}/cs2bot" "${BUILD_DIR}/cs2bot"
cp "${ROOT_DIR}/requirements.txt" "${BUILD_DIR}/requirements.txt"
(cd "${BUILD_DIR}" && zip -qr "${OUTPUT_PATH}" cs2bot requirements.txt -x '*/__pycache__/*' '*.pyc')
echo "${OUTPUT_PATH}"

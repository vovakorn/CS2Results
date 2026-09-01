#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUTPUT_PATH="${1:-${ROOT_DIR}/dist/cs2-social-publish.zip}"
BUILD_DIR="$(mktemp -d)"

cleanup() {
  rm -rf "${BUILD_DIR}"
}
trap cleanup EXIT

mkdir -p "${BUILD_DIR}/cs2bot" "${BUILD_DIR}/xray" "$(dirname "${OUTPUT_PATH}")"
cp "${ROOT_DIR}/cs2bot/__init__.py" "${BUILD_DIR}/cs2bot/__init__.py"
cp "${ROOT_DIR}/cs2bot/social_publish.py" "${BUILD_DIR}/cs2bot/social_publish.py"
cp "${ROOT_DIR}/cs2bot/instagram_publish.py" "${BUILD_DIR}/cs2bot/instagram_publish.py"
cp "${ROOT_DIR}/cs2bot/threads_publish.py" "${BUILD_DIR}/cs2bot/threads_publish.py"
cp "${ROOT_DIR}/cs2bot/xray_proxy.py" "${BUILD_DIR}/cs2bot/xray_proxy.py"
cp "${ROOT_DIR}/requirements.txt" "${BUILD_DIR}/requirements.txt"
"${ROOT_DIR}/scripts/fetch_xray_core.sh"
cp "${ROOT_DIR}/build/xray-core/v26.6.27/xray" "${BUILD_DIR}/xray/xray"
chmod 755 "${BUILD_DIR}/xray/xray"

(
  cd "${BUILD_DIR}"
  zip -qr "${OUTPUT_PATH}" cs2bot requirements.txt xray
)

echo "Built ${OUTPUT_PATH}"

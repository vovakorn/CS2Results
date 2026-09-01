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
cp "${ROOT_DIR}/cs2bot/xray_proxy.py" "${BUILD_DIR}/cs2bot/xray_proxy.py"
cp "${ROOT_DIR}/requirements-social-oauth.txt" "${BUILD_DIR}/requirements.txt"

if [[ "${XRAY_ENABLED:-0}" == "1" ]]; then
  "${ROOT_DIR}/scripts/fetch_xray_core.sh"
  mkdir -p "${BUILD_DIR}/xray"
  cp "${ROOT_DIR}/build/xray-core/v26.6.27/xray" "${BUILD_DIR}/xray/xray"
  chmod 755 "${BUILD_DIR}/xray/xray"
  ZIP_CONTENT=(cs2bot requirements.txt xray)
else
  ZIP_CONTENT=(cs2bot requirements.txt)
fi

(
  cd "${BUILD_DIR}"
  zip -qr "${OUTPUT_PATH}" "${ZIP_CONTENT[@]}"
)

echo "Built ${OUTPUT_PATH}"

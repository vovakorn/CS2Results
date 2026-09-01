#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="${ROOT_DIR}/build/function"
DIST_DIR="${ROOT_DIR}/dist"
ZIP_PATH="${DIST_DIR}/function.zip"

rm -rf "${BUILD_DIR}" "${ZIP_PATH}"
mkdir -p "${BUILD_DIR}" "${DIST_DIR}"

cp -R "${ROOT_DIR}/cs2bot" "${BUILD_DIR}/cs2bot"
cp "${ROOT_DIR}/runtime.txt" "${BUILD_DIR}/runtime.txt"
cp "${ROOT_DIR}/requirements.txt" "${BUILD_DIR}/requirements.txt"
if [ "${XRAY_ENABLED:-0}" = "1" ]; then
  "${ROOT_DIR}/scripts/fetch_xray_core.sh"
  mkdir -p "${BUILD_DIR}/xray"
  cp "${ROOT_DIR}/build/xray-core/v26.6.27/xray" "${BUILD_DIR}/xray/xray"
  chmod 755 "${BUILD_DIR}/xray/xray"
fi
if [ -f "${ROOT_DIR}/tier1_filter.json" ]; then
  cp "${ROOT_DIR}/tier1_filter.json" "${BUILD_DIR}/tier1_filter.json"
fi

(cd "${BUILD_DIR}" && zip -qr "${ZIP_PATH}" . -x "*/__pycache__/*" "*.pyc")

echo "${ZIP_PATH}"

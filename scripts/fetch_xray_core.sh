#!/usr/bin/env bash
set -euo pipefail

XRAY_VERSION="v26.6.27"
XRAY_SHA256="b3e5902d06d6282fe53cfa2fc426058b9aeaa429b2c812e20887cd47f26d08bf"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET_DIR="${ROOT_DIR}/build/xray-core/${XRAY_VERSION}"
TARGET_BINARY="${TARGET_DIR}/xray"

if [[ -x "${TARGET_BINARY}" ]]; then
  exit 0
fi

WORK_DIR="$(mktemp -d)"
cleanup() { rm -rf "${WORK_DIR}"; }
trap cleanup EXIT

ARCHIVE="${WORK_DIR}/Xray-linux-64.zip"
curl --fail --silent --show-error --location \
  "https://github.com/XTLS/Xray-core/releases/download/${XRAY_VERSION}/Xray-linux-64.zip" \
  --output "${ARCHIVE}"
ACTUAL_SHA256="$(shasum -a 256 "${ARCHIVE}" | awk '{print $1}')"
[[ "${ACTUAL_SHA256}" == "${XRAY_SHA256}" ]] || {
  echo "Xray archive checksum mismatch" >&2
  exit 1
}

mkdir -p "${TARGET_DIR}"
unzip -q "${ARCHIVE}" -d "${TARGET_DIR}"
chmod 755 "${TARGET_BINARY}"

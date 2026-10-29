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
cp "${ROOT_DIR}/cs2bot/media_cards.py" "${BUILD_DIR}/cs2bot/media_cards.py"
for module in tournament_visuals tournament_identity tournament_branding tournament_theme_patterns tournament_preview; do
  cp "${ROOT_DIR}/cs2bot/${module}.py" "${BUILD_DIR}/cs2bot/${module}.py"
done
cp -R "${ROOT_DIR}/cs2bot/assets" "${BUILD_DIR}/cs2bot/assets"
cp -R "${ROOT_DIR}/cs2bot/match_sources" "${BUILD_DIR}/cs2bot/match_sources"
mkdir -p "${BUILD_DIR}/data"
cp "${ROOT_DIR}/data/tournament_preview_profiles.json" "${BUILD_DIR}/data/tournament_preview_profiles.json"
cp "${ROOT_DIR}/data/tournament_branding_themes.json" "${BUILD_DIR}/data/tournament_branding_themes.json"
cp "${ROOT_DIR}/tier1_filter.json" "${BUILD_DIR}/tier1_filter.json"
cp "${ROOT_DIR}/requirements.txt" "${BUILD_DIR}/requirements.txt"
"${ROOT_DIR}/scripts/fetch_xray_core.sh"
cp "${ROOT_DIR}/build/xray-core/v26.6.27/xray" "${BUILD_DIR}/xray/xray"
chmod 755 "${BUILD_DIR}/xray/xray"

(
  cd "${BUILD_DIR}"
  zip -qr "${BUILD_DIR}/package.zip" cs2bot requirements.txt xray data tier1_filter.json -x '*/__pycache__/*' '*.pyc'
)
mv "${BUILD_DIR}/package.zip" "${OUTPUT_PATH}"

echo "Built ${OUTPUT_PATH}"

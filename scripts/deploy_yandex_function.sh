#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ZIP_PATH="$("${ROOT_DIR}/scripts/build_function_zip.sh")"

: "${YC_FUNCTION_NAME:?Set YC_FUNCTION_NAME}"
: "${YC_SERVICE_ACCOUNT_ID:?Set YC_SERVICE_ACCOUNT_ID}"

yc serverless function version create \
  --function-name "${YC_FUNCTION_NAME}" \
  --runtime python311 \
  --entrypoint cs2bot.main.handler \
  --memory "${YC_MEMORY:-256m}" \
  --execution-timeout "${YC_TIMEOUT:-60s}" \
  --service-account-id "${YC_SERVICE_ACCOUNT_ID}" \
  --source-path "${ZIP_PATH}"

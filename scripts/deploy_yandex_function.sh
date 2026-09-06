#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
YC_BIN="${YC_BIN:-yc}"
JQ_BIN="${JQ_BIN:-jq}"
PYTHON_BIN="${YC_PYTHON_BIN:-python3}"
BUILD_SCRIPT="${YC_BUILD_SCRIPT:-${ROOT_DIR}/scripts/build_function_zip.sh}"

FUNCTION_ID="${YC_FUNCTION_ID:-}"
EXPECTED_FOLDER_ID="${YC_FOLDER_ID:-b1g5j8hk4gjas2vpvgqr}"
PRODUCTION_TAG="${YC_PRODUCTION_TAG:-production}"
CANDIDATE_TAG="${YC_CANDIDATE_TAG:-candidate}"
ROLLBACK_TAG="${YC_ROLLBACK_TAG:-rollback}"
DRY_RUN_PAYLOAD="${YC_DRY_RUN_PAYLOAD:-}"
if [[ -z "${DRY_RUN_PAYLOAD}" ]]; then
  DRY_RUN_PAYLOAD='{"limit":1,"dry_run":true}'
fi
LIQUIPEDIA_SECRET_ID="${YC_LIQUIPEDIA_SECRET_ID:-}"
LIQUIPEDIA_SECRET_VERSION_ID="${YC_LIQUIPEDIA_SECRET_VERSION_ID:-}"
LIQUIPEDIA_SECRET_KEY="${YC_LIQUIPEDIA_SECRET_KEY:-LIQUIPEDIA_API_KEY}"
LIQUIPEDIA_SHADOW_OVERRIDE="${YC_ENABLE_LIQUIPEDIA_SHADOW:-}"
LIQUIPEDIA_FINAL_CARDS_OVERRIDE="${YC_ENABLE_LIQUIPEDIA_FINAL_CARDS:-}"
TELEGRAM_PROXY_SECRET_ID="${YC_TELEGRAM_PROXY_SECRET_ID:-}"
TELEGRAM_PROXY_SECRET_VERSION_ID="${YC_TELEGRAM_PROXY_SECRET_VERSION_ID:-}"
TELEGRAM_PROXY_SECRET_KEY="${YC_TELEGRAM_PROXY_SECRET_KEY:-TELEGRAM_PROXY_URL}"
TELEGRAM_TOKEN_SECRET_ID="${YC_TELEGRAM_TOKEN_SECRET_ID:-}"
TELEGRAM_TOKEN_SECRET_VERSION_ID="${YC_TELEGRAM_TOKEN_SECRET_VERSION_ID:-}"
TELEGRAM_TOKEN_SECRET_KEY="${YC_TELEGRAM_TOKEN_SECRET_KEY:-TELEGRAM_TOKEN}"
INSTAGRAM_PUBLISHING_OVERRIDE="${YC_ENABLE_INSTAGRAM_PUBLISHING:-}"
INSTAGRAM_MEDIA_BUCKET_OVERRIDE="${YC_INSTAGRAM_MEDIA_BUCKET:-}"
INSTAGRAM_MEDIA_PUBLIC_BASE_URL_OVERRIDE="${YC_INSTAGRAM_MEDIA_PUBLIC_BASE_URL:-}"
INSTAGRAM_LOCKBOX_SECRET_ID_OVERRIDE="${YC_INSTAGRAM_LOCKBOX_SECRET_ID:-}"
THREADS_PUBLISHING_OVERRIDE="${YC_ENABLE_THREADS_PUBLISHING:-}"
THREADS_MEDIA_BUCKET_OVERRIDE="${YC_THREADS_MEDIA_BUCKET:-}"
THREADS_MEDIA_PUBLIC_BASE_URL_OVERRIDE="${YC_THREADS_MEDIA_PUBLIC_BASE_URL:-}"
THREADS_LOCKBOX_SECRET_ID_OVERRIDE="${YC_THREADS_LOCKBOX_SECRET_ID:-}"
XRAY_SECRET_ID="${YC_XRAY_SECRET_ID:-}"
XRAY_SECRET_VERSION_ID="${YC_XRAY_SECRET_VERSION_ID:-}"
XRAY_SECRET_KEY="${YC_XRAY_SECRET_KEY:-XRAY_CONFIG_JSON}"
FUNCTION_PACKAGE_BUCKET="${YC_FUNCTION_PACKAGE_BUCKET:-}"
TARGET_EXECUTION_TIMEOUT="${YC_EXECUTION_TIMEOUT:-120s}"
DIRECT_UPLOAD_MAX_BYTES="${YC_DIRECT_UPLOAD_MAX_BYTES:-3500000}"
EXPECTED_TRIGGER_COUNT="${YC_EXPECTED_TRIGGER_COUNT:-5}"
PACKAGE_PREFIX="${YC_FUNCTION_PACKAGE_PREFIX:-function-packages}"
MANIFEST_PREFIX="${YC_RELEASE_MANIFEST_PREFIX:-release-manifests}"
RELEASE_DIR="${YC_RELEASE_DIR:-${ROOT_DIR}/dist/releases}"
PACKAGE_LIFECYCLE_MAX_DAYS="${YC_PACKAGE_LIFECYCLE_MAX_DAYS:-30}"
SMOKE_LOG_WAIT_SECONDS="${YC_SMOKE_LOG_WAIT_SECONDS:-5}"
SMOKE_INVOKE_TIMEOUT_SECONDS="${YC_SMOKE_INVOKE_TIMEOUT_SECONDS:-150}"
SMOKE_LOG_TIMEOUT_SECONDS="${YC_SMOKE_LOG_TIMEOUT_SECONDS:-30}"

readonly -a REQUIRED_ENVIRONMENT=(
  ALERT_COOLDOWN_SECONDS
  BOT_MODE
  DELIVERY_CLAIM_TTL_SECONDS
  DISPLAY_TIMEZONE
  ENABLE_LIQUIPEDIA_FALLBACK
  MATCH_SOURCE
  MAX_SOURCE_FUTURE_SKEW_HOURS
  MAX_SOURCE_RESPONSE_BYTES
  MAX_SOURCE_STALENESS_HOURS
  OBJECT_STORAGE_BUCKET
  OBJECT_STORAGE_ENDPOINT
  REQUEST_TIMEOUT_SECONDS
  TELEGRAM_ADMIN_CHAT_ID
  TELEGRAM_CHAT_ID
  TELEGRAM_MEDIA_CARDS
  TELEGRAM_SPOILERS
)
readonly -a REQUIRED_SECRETS=(
  AWS_ACCESS_KEY_ID
  AWS_SECRET_ACCESS_KEY
  PANDASCORE_API_TOKEN
  TELEGRAM_TOKEN
)

usage() {
  cat <<'EOF'
Usage:
  YC_FUNCTION_ID=<id> scripts/deploy_yandex_function.sh check
  YC_FUNCTION_ID=<id> scripts/deploy_yandex_function.sh candidate
  YC_FUNCTION_ID=<id> YC_PROMOTE_APPROVED=1 scripts/deploy_yandex_function.sh promote <manifest.json>
  YC_FUNCTION_ID=<id> scripts/deploy_yandex_function.sh smoke <manifest.json>
  YC_FUNCTION_ID=<id> YC_ROLLBACK_APPROVED=1 scripts/deploy_yandex_function.sh rollback <manifest.json>

Commands:
  check      Read-only validation of the production version and all timer triggers.
  candidate  Build once, create and validate an immutable candidate, then write
             a release manifest. Production is unchanged.
  promote    Promote the exact candidate recorded in a manifest without rebuilding.
             Runs a production smoke and rolls back automatically if it fails.
  smoke      Re-run the production smoke recorded by the release process.
  rollback   Move production back to the previous version in the manifest and
             verify the rollback with the same smoke checks.

The script never reads Lockbox values. It copies only secret references from the
version currently tagged "production". Exactly YC_EXPECTED_TRIGGER_COUNT timer
triggers must invoke that tag; otherwise the script stops before creating a version.

Archives larger than 3,500,000 bytes require YC_FUNCTION_PACKAGE_BUCKET. The
bucket must be private and have an enabled expiration lifecycle for
function-packages/ no longer than YC_PACKAGE_LIFECYCLE_MAX_DAYS.

For Liquipedia overrides, use YC_ENABLE_LIQUIPEDIA_SHADOW or
YC_ENABLE_LIQUIPEDIA_FINAL_CARDS. Only Lockbox references are passed; the API
key value is never read by this script.

To add or rotate an optional Telegram egress proxy, pass all three
YC_TELEGRAM_PROXY_SECRET_* reference fields. The proxy URL is never read.

To rotate the bot token, pass all three YC_TELEGRAM_TOKEN_SECRET_* reference
fields. The token value is never read.

Instagram publishing requires YC_ENABLE_INSTAGRAM_PUBLISHING=1,
YC_INSTAGRAM_MEDIA_BUCKET, YC_INSTAGRAM_MEDIA_PUBLIC_BASE_URL,
YC_INSTAGRAM_LOCKBOX_SECRET_ID, and YC_XRAY_SECRET_{ID,VERSION_ID}.

Threads publishing accepts YC_ENABLE_THREADS_PUBLISHING,
YC_THREADS_MEDIA_BUCKET, YC_THREADS_MEDIA_PUBLIC_BASE_URL and
YC_THREADS_LOCKBOX_SECRET_ID. These are ordinary environment variables; the
Threads OAuth payload is never passed to this script.
EOF
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "Required command not found: $1"
}

jq_from() {
  local json="$1"
  local filter="$2"
  printf '%s' "${json}" | "${JQ_BIN}" -er "${filter}"
}

is_nonempty_json_value() {
  local json="$1"
  local filter="$2"
  printf '%s' "${json}" | "${JQ_BIN}" -e "${filter} | select(. != null and . != false and . != \"\" and . != \"0\" and . != {} and . != [])" >/dev/null
}

utc_now() {
  date -u +%Y-%m-%dT%H:%M:%SZ
}

file_size_bytes() {
  local path="$1"
  wc -c <"${path}" | tr -d '[:space:]'
}

sha256_file() {
  local path="$1"
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "${path}" | awk '{print $1}'
  elif command -v sha256sum >/dev/null 2>&1; then
    sha256sum "${path}" | awk '{print $1}'
  else
    die "Required command not found: shasum or sha256sum"
  fi
}

validate_positive_integer() {
  local name="$1"
  local value="$2"
  [[ "${value}" =~ ^[1-9][0-9]*$ ]] || die "${name} must be a positive integer"
}

validate_upload_limit() {
  validate_positive_integer "YC_DIRECT_UPLOAD_MAX_BYTES" "${DIRECT_UPLOAD_MAX_BYTES}"
  (( DIRECT_UPLOAD_MAX_BYTES <= 3500000 )) \
    || die "YC_DIRECT_UPLOAD_MAX_BYTES cannot exceed the 3500000-byte direct upload limit"
}

acquire_release_lock() {
  [[ "${FUNCTION_ID}" =~ ^[a-zA-Z0-9_-]+$ ]] || die "Set a valid YC_FUNCTION_ID"
  mkdir -p "${ROOT_DIR}/dist/release-locks"
  RELEASE_LOCK="${ROOT_DIR}/dist/release-locks/${FUNCTION_ID}"
  mkdir "${RELEASE_LOCK}" 2>/dev/null \
    || die "Another local release command is active. Lock: ${RELEASE_LOCK} (remove only after confirming its process stopped)"
  trap 'rmdir "${RELEASE_LOCK}" 2>/dev/null || true' EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
}

run_with_timeout() {
  local timeout_seconds="$1"
  shift

  "${PYTHON_BIN}" -c '
import subprocess
import sys

try:
    result = subprocess.run(sys.argv[2:], timeout=int(sys.argv[1]), check=False)
except subprocess.TimeoutExpired:
    raise SystemExit(124)
raise SystemExit(result.returncode)
' "${timeout_seconds}" "$@"
}

validate_package_bucket() {
  local bucket_json
  local bucket_folder_id
  local public_policy

  [[ -n "${FUNCTION_PACKAGE_BUCKET}" ]] || die "Set YC_FUNCTION_PACKAGE_BUCKET"
  bucket_json="$("${YC_BIN}" storage bucket get "${FUNCTION_PACKAGE_BUCKET}" --full --format json)" \
    || die "Cannot read package bucket ${FUNCTION_PACKAGE_BUCKET}"
  bucket_folder_id="$(jq_from "${bucket_json}" '.folder_id')"
  [[ "${bucket_folder_id}" == "${EXPECTED_FOLDER_ID}" ]] \
    || die "Package bucket belongs to folder ${bucket_folder_id}, expected ${EXPECTED_FOLDER_ID}"

  printf '%s' "${bucket_json}" | "${JQ_BIN}" -e '
    (.anonymous_access_flags // {}) as $flags
    | (($flags.read // false) == false)
      and (($flags.list // false) == false)
      and (($flags.config_read // false) == false)
  ' >/dev/null || die "Package bucket ${FUNCTION_PACKAGE_BUCKET} has anonymous access enabled"

  if ! printf '%s' "${bucket_json}" | "${JQ_BIN}" -e '
    [(.acl.grants // [])[] | .. | strings | ascii_downcase]
    | any(. == "grant_type_all_users"
          or . == "grant-type-all-users"
          or . == "grant_type_all_authenticated_users"
          or . == "grant-type-all-authenticated-users")
    | not
  ' >/dev/null; then
    die "Package bucket ${FUNCTION_PACKAGE_BUCKET} has a public or invalid ACL grant"
  fi

  public_policy="$(printf '%s' "${bucket_json}" | "${JQ_BIN}" -r '
    def public_principal:
      . == "*"
      or (type == "array" and any(.[]; public_principal))
      or (type == "object" and any(.[]; public_principal));
    (.policy // null) as $raw
    | if $raw == null or $raw == "" then false
      else (if ($raw | type) == "string" then ($raw | fromjson) else $raw end)
        | (.Statement // .statement // [])
        | if type == "object" then [.] elif type == "array" then . else error("invalid policy") end
        | any(((.Effect // .effect // "") | ascii_downcase) == "allow"
              and (has("NotPrincipal") or has("notPrincipal")
                   or ((.Principal // .principal // null) | public_principal)))
      end
  ' 2>/dev/null)" || die "Cannot validate package bucket policy"
  if [[ "${public_policy}" != "false" ]]; then
    die "Package bucket ${FUNCTION_PACKAGE_BUCKET} has a public bucket policy"
  fi

  printf '%s' "${bucket_json}" | "${JQ_BIN}" -e \
    --arg prefix "${PACKAGE_PREFIX}/" \
    --argjson max_days "${PACKAGE_LIFECYCLE_MAX_DAYS}" '
      (.lifecycle_rules // .lifecycleRules // [])
      | any(
          (.enabled == true)
          and ((.filter.prefix // .prefix // "") == $prefix)
          and ((.filter // {} | keys) - ["prefix"] | length == 0)
          and (((.expiration.days // .expiration.after_days // 0) | tonumber) > 0)
          and (((.expiration.days // .expiration.after_days // 0) | tonumber) <= $max_days)
        )
    ' >/dev/null \
    || die "Package bucket ${FUNCTION_PACKAGE_BUCKET} needs an enabled ${PACKAGE_PREFIX}/ expiration lifecycle of at most ${PACKAGE_LIFECYCLE_MAX_DAYS} days"
  if [[ "$(printf '%s' "${bucket_json}" | "${JQ_BIN}" -r '.versioning // "VERSIONING_DISABLED"')" != "VERSIONING_DISABLED" ]]; then
    die "Package lifecycle template requires an unversioned bucket; configure noncurrent-version retention before using a versioned bucket"
  fi
}

write_manifest() {
  local manifest_path="$1"
  local filter="$2"
  local temp_path
  shift 2

  mkdir -p "$(dirname "${manifest_path}")"
  temp_path="$(mktemp "${manifest_path}.tmp.XXXXXX")"
  if ! "${JQ_BIN}" "$@" "${filter}" "${manifest_path}" >"${temp_path}"; then
    rm -f "${temp_path}"
    return 1
  fi
  mv "${temp_path}" "${manifest_path}"
}

upload_manifest() {
  local manifest_path="$1"
  local candidate_id
  local manifest_key

  [[ -n "${FUNCTION_PACKAGE_BUCKET}" ]] || return 0
  candidate_id="$("${JQ_BIN}" -er '.candidate_version_id' "${manifest_path}")"
  manifest_key="${MANIFEST_PREFIX}/${candidate_id}.json"
  "${YC_BIN}" storage s3api put-object \
    --bucket "${FUNCTION_PACKAGE_BUCKET}" \
    --key "${manifest_key}" \
    --body "${manifest_path}" \
    --acl private \
    --content-type application/json >/dev/null
}

validate_manifest() {
  local manifest_path="$1"

  [[ -f "${manifest_path}" ]] || die "Release manifest not found: ${manifest_path}"
  "${JQ_BIN}" -e \
    --arg function_id "${FUNCTION_ID}" \
    --arg folder_id "${EXPECTED_FOLDER_ID}" \
    --arg production_tag "${PRODUCTION_TAG}" \
    --arg candidate_tag "${CANDIDATE_TAG}" \
    --arg rollback_tag "${ROLLBACK_TAG}" '
      .schema_version == 1
      and .function_id == $function_id
      and .folder_id == $folder_id
      and (.git_sha | type == "string" and test("^[0-9a-f]{40}$"))
      and (.archive_sha256 | type == "string" and test("^[0-9a-f]{64}$"))
      and (.archive_size_bytes | type == "number" and . > 0)
      and (.previous_version_id | type == "string" and length > 0)
      and (.candidate_version_id | type == "string" and length > 0)
      and .candidate_version_id != .previous_version_id
      and (.git_dirty | type == "boolean")
      and .tags == {production: $production_tag, candidate: $candidate_tag, rollback: $rollback_tag}
      and (.smoke_payload | type == "object" and .dry_run == true and (has("messages") | not))
    ' "${manifest_path}" >/dev/null || die "Release manifest is invalid: ${manifest_path}"
  DRY_RUN_PAYLOAD="$("${JQ_BIN}" -c '.smoke_payload' "${manifest_path}")"
}

validate_required_configuration() {
  local version_json="$1"
  local name

  for name in "${REQUIRED_ENVIRONMENT[@]}"; do
    printf '%s' "${version_json}" | "${JQ_BIN}" -e --arg name "${name}" '(.environment // {}) | has($name)' >/dev/null \
      || die "Production version is missing environment variable: ${name}"
  done

  for name in "${REQUIRED_SECRETS[@]}"; do
    printf '%s' "${version_json}" | "${JQ_BIN}" -e --arg name "${name}" \
      'any((.secrets // [])[]; .environment_variable == $name and .id != "" and .version_id != "" and .key != "")' >/dev/null \
      || die "Production version is missing pinned Lockbox binding: ${name}"
  done

  if printf '%s' "${version_json}" | "${JQ_BIN}" -e '
    (.environment // {}) as $environment
    | [($environment.ENABLE_LIQUIPEDIA_FALLBACK // "0"),
       ($environment.ENABLE_LIQUIPEDIA_SHADOW // "0"),
       ($environment.ENABLE_LIQUIPEDIA_FINAL_CARDS // "0")]
    | any(. == "1" or (ascii_downcase == "true") or (ascii_downcase == "yes"))
  ' >/dev/null; then
    printf '%s' "${version_json}" | "${JQ_BIN}" -e \
      'any((.secrets // [])[]; .environment_variable == "LIQUIPEDIA_API_KEY" and .id != "" and .version_id != "" and .key != "")' >/dev/null \
      || die "Liquipedia is enabled but production is missing pinned Lockbox binding: LIQUIPEDIA_API_KEY"
  fi

  printf '%s' "${version_json}" | "${JQ_BIN}" -e '
    [(.secrets // [])[].environment_variable] as $names
    | ($names | length) == ($names | unique | length)
  ' >/dev/null || die "Production version has duplicate secret environment variables"

  printf '%s' "${version_json}" | "${JQ_BIN}" -e '
    ((.environment // {}) | keys) as $environment
    | [(.secrets // [])[].environment_variable] as $secrets
    | (($environment - ($environment - $secrets)) | length) == 0
  ' >/dev/null || die "A variable is defined both directly and through Lockbox"

  local unsupported
  unsupported="$(printf '%s' "${version_json}" | "${JQ_BIN}" -r '
    [
      ["connectivity", .connectivity],
      ["named_service_accounts", .named_service_accounts],
      ["storage_mounts", .storage_mounts],
      ["mounts", .mounts],
      ["tmpfs_size", .tmpfs_size],
      ["async_invocation_config", .async_invocation_config]
    ]
    | map(select(.[1] != null and .[1] != false and .[1] != "" and .[1] != "0" and .[1] != {} and .[1] != []))
    | map(.[0])
    | join(", ")
  ')"
  [[ -z "${unsupported}" ]] || die "Unsupported production settings would be lost: ${unsupported}"
}

validate_liquipedia_override() {
  # The key defaults to LIQUIPEDIA_API_KEY, so ID and version must either both
  # be present or both be absent.
  if [[ -n "${LIQUIPEDIA_SECRET_ID}" || -n "${LIQUIPEDIA_SECRET_VERSION_ID}" ]]; then
    [[ -n "${LIQUIPEDIA_SECRET_ID}" && -n "${LIQUIPEDIA_SECRET_VERSION_ID}" && -n "${LIQUIPEDIA_SECRET_KEY}" ]] \
      || die "Set all YC_LIQUIPEDIA_SECRET_* reference fields together"
  fi

  if [[ -n "${LIQUIPEDIA_SHADOW_OVERRIDE}" ]]; then
    case "${LIQUIPEDIA_SHADOW_OVERRIDE}" in
      1|true|TRUE|yes|YES) LIQUIPEDIA_SHADOW_OVERRIDE="1" ;;
      0|false|FALSE|no|NO) LIQUIPEDIA_SHADOW_OVERRIDE="0" ;;
      *) die "YC_ENABLE_LIQUIPEDIA_SHADOW must be a boolean" ;;
    esac
  fi

  if [[ -n "${LIQUIPEDIA_FINAL_CARDS_OVERRIDE}" ]]; then
    case "${LIQUIPEDIA_FINAL_CARDS_OVERRIDE}" in
      1|true|TRUE|yes|YES) LIQUIPEDIA_FINAL_CARDS_OVERRIDE="1" ;;
      0|false|FALSE|no|NO) LIQUIPEDIA_FINAL_CARDS_OVERRIDE="0" ;;
      *) die "YC_ENABLE_LIQUIPEDIA_FINAL_CARDS must be a boolean" ;;
    esac
  fi

  if [[ "${LIQUIPEDIA_SHADOW_OVERRIDE}" == "1" || "${LIQUIPEDIA_FINAL_CARDS_OVERRIDE}" == "1" ]] && [[ -z "${LIQUIPEDIA_SECRET_ID}" ]]; then
    printf '%s' "${PRODUCTION_JSON}" | "${JQ_BIN}" -e \
      'any((.secrets // [])[]; .environment_variable == "LIQUIPEDIA_API_KEY")' >/dev/null \
      || die "Enabling Liquipedia requires a pinned LIQUIPEDIA_API_KEY Lockbox reference"
  fi
}

validate_telegram_proxy_override() {
  if [[ -n "${TELEGRAM_PROXY_SECRET_ID}" || -n "${TELEGRAM_PROXY_SECRET_VERSION_ID}" ]]; then
    [[ -n "${TELEGRAM_PROXY_SECRET_ID}" && -n "${TELEGRAM_PROXY_SECRET_VERSION_ID}" && -n "${TELEGRAM_PROXY_SECRET_KEY}" ]] \
      || die "Set all YC_TELEGRAM_PROXY_SECRET_* reference fields together"
  fi
}

validate_telegram_token_override() {
  if [[ -n "${TELEGRAM_TOKEN_SECRET_ID}" || -n "${TELEGRAM_TOKEN_SECRET_VERSION_ID}" ]]; then
    [[ -n "${TELEGRAM_TOKEN_SECRET_ID}" && -n "${TELEGRAM_TOKEN_SECRET_VERSION_ID}" && -n "${TELEGRAM_TOKEN_SECRET_KEY}" ]] \
      || die "Set all YC_TELEGRAM_TOKEN_SECRET_* reference fields together"
  fi
}

validate_instagram_override() {
  if [[ -z "${INSTAGRAM_PUBLISHING_OVERRIDE}" ]]; then
    return 0
  fi
  case "${INSTAGRAM_PUBLISHING_OVERRIDE}" in
    1|true|TRUE|yes|YES) INSTAGRAM_PUBLISHING_OVERRIDE="1" ;;
    0|false|FALSE|no|NO) INSTAGRAM_PUBLISHING_OVERRIDE="0" ;;
    *) die "YC_ENABLE_INSTAGRAM_PUBLISHING must be a boolean" ;;
  esac
  if [[ "${INSTAGRAM_PUBLISHING_OVERRIDE}" == "1" ]]; then
    [[ -n "${INSTAGRAM_MEDIA_BUCKET_OVERRIDE}" && -n "${INSTAGRAM_MEDIA_PUBLIC_BASE_URL_OVERRIDE}" && -n "${INSTAGRAM_LOCKBOX_SECRET_ID_OVERRIDE}" ]] \
      || die "Enabling Instagram requires its media bucket, public URL, and Lockbox secret ID"
    [[ -n "${XRAY_SECRET_ID}" && -n "${XRAY_SECRET_VERSION_ID}" && -n "${XRAY_SECRET_KEY}" ]] \
      || die "Enabling Instagram requires all YC_XRAY_SECRET_* reference fields"
  fi
}

validate_threads_override() {
  if [[ -n "${THREADS_PUBLISHING_OVERRIDE}" ]]; then
    case "${THREADS_PUBLISHING_OVERRIDE}" in
      1|true|TRUE|yes|YES) THREADS_PUBLISHING_OVERRIDE="1" ;;
      0|false|FALSE|no|NO) THREADS_PUBLISHING_OVERRIDE="0" ;;
      *) die "YC_ENABLE_THREADS_PUBLISHING must be a boolean" ;;
    esac
  fi
  if [[ -n "${THREADS_PUBLISHING_OVERRIDE}${THREADS_MEDIA_BUCKET_OVERRIDE}${THREADS_MEDIA_PUBLIC_BASE_URL_OVERRIDE}${THREADS_LOCKBOX_SECRET_ID_OVERRIDE}" ]]; then
    [[ -n "${THREADS_PUBLISHING_OVERRIDE}" && -n "${THREADS_MEDIA_BUCKET_OVERRIDE}" && -n "${THREADS_MEDIA_PUBLIC_BASE_URL_OVERRIDE}" && -n "${THREADS_LOCKBOX_SECRET_ID_OVERRIDE}" ]] \
      || die "Set all YC_THREADS_* publishing settings together"
  fi
}

validate_trigger_tags() {
  local triggers_json="$1"
  local bad_triggers
  local matching_count

  matching_count="$(printf '%s' "${triggers_json}" | "${JQ_BIN}" -r --arg function_id "${FUNCTION_ID}" '
    [ .[] | select(any((.rule.timer // {}) | .. | objects; .function_id? == $function_id)) ] | length
  ')"
  [[ "${matching_count}" == "${EXPECTED_TRIGGER_COUNT}" ]] \
    || die "Expected ${EXPECTED_TRIGGER_COUNT} timer triggers for function ${FUNCTION_ID}, found ${matching_count}"

  bad_triggers="$(printf '%s' "${triggers_json}" | "${JQ_BIN}" -r \
    --arg function_id "${FUNCTION_ID}" \
    --arg production_tag "${PRODUCTION_TAG}" '
      .[]
      | select(any((.rule.timer // {}) | .. | objects; .function_id? == $function_id and ((.function_tag? // "") != $production_tag)))
      | .name
    ')"

  if [[ -n "${bad_triggers}" ]]; then
    printf 'Triggers not pinned to tag %s:\n%s\n' "${PRODUCTION_TAG}" "${bad_triggers}" >&2
    die "Pin every production trigger to ${PRODUCTION_TAG} before deploying"
  fi
}

build_create_arguments() {
  local version_json="$1"
  local zip_path="$2"
  local environment_csv
  local memory_bytes
  local value

  CREATE_ARGS=(
    serverless function version create
    --function-id "${FUNCTION_ID}"
    --runtime "$(jq_from "${version_json}" '.runtime')"
    --entrypoint "$(jq_from "${version_json}" '.entrypoint')"
    --execution-timeout "${TARGET_EXECUTION_TIMEOUT}"
    --tags "${CANDIDATE_TAG}"
    --description "release git=${RELEASE_GIT_SHA:0:12} sha256=${FUNCTION_PACKAGE_SHA256} previous=$(jq_from "${version_json}" '.id')"
    --format json
  )

  if [[ -n "${FUNCTION_PACKAGE_BUCKET}" ]]; then
    [[ -n "${FUNCTION_PACKAGE_OBJECT}" && -n "${FUNCTION_PACKAGE_SHA256}" ]] \
      || die "Function package object metadata is unavailable"
    CREATE_ARGS+=(
      --package-bucket-name "${FUNCTION_PACKAGE_BUCKET}"
      --package-object-name "${FUNCTION_PACKAGE_OBJECT}"
      --package-sha256 "${FUNCTION_PACKAGE_SHA256}"
    )
  else
    CREATE_ARGS+=(--source-path "${zip_path}")
  fi

  memory_bytes="$(jq_from "${version_json}" '.resources.memory')"
  [[ "${memory_bytes}" =~ ^[0-9]+$ ]] || die "Production memory value is invalid"
  (( memory_bytes % 1048576 == 0 )) || die "Production memory is not a whole number of MiB"
  CREATE_ARGS+=(--memory "$((memory_bytes / 1048576))MB")

  value="$(jq_from "${version_json}" '.service_account_id')"
  CREATE_ARGS+=(--service-account-id "${value}")

  value="$(printf '%s' "${version_json}" | "${JQ_BIN}" -r '.concurrency // empty')"
  [[ -z "${value}" ]] || CREATE_ARGS+=(--concurrency "${value}")

  environment_csv="$(printf '%s' "${version_json}" | "${JQ_BIN}" -r --arg shadow "${LIQUIPEDIA_SHADOW_OVERRIDE}" --arg final_cards "${LIQUIPEDIA_FINAL_CARDS_OVERRIDE}" --arg instagram_enabled "${INSTAGRAM_PUBLISHING_OVERRIDE}" --arg instagram_bucket "${INSTAGRAM_MEDIA_BUCKET_OVERRIDE}" --arg instagram_public_base "${INSTAGRAM_MEDIA_PUBLIC_BASE_URL_OVERRIDE}" --arg instagram_lockbox "${INSTAGRAM_LOCKBOX_SECRET_ID_OVERRIDE}" --arg threads_enabled "${THREADS_PUBLISHING_OVERRIDE}" --arg threads_bucket "${THREADS_MEDIA_BUCKET_OVERRIDE}" --arg threads_public_base "${THREADS_MEDIA_PUBLIC_BASE_URL_OVERRIDE}" --arg threads_lockbox "${THREADS_LOCKBOX_SECRET_ID_OVERRIDE}" '
    (.environment // {})
    | if $shadow == "" then . else . + {"ENABLE_LIQUIPEDIA_SHADOW": $shadow} end
    | if $final_cards == "" then . else . + {"ENABLE_LIQUIPEDIA_FINAL_CARDS": $final_cards} end
    | if $instagram_enabled == "" then . else . + {"ENABLE_INSTAGRAM_PUBLISHING": $instagram_enabled} end
    | if $instagram_bucket == "" then . else . + {"INSTAGRAM_MEDIA_BUCKET": $instagram_bucket} end
    | if $instagram_public_base == "" then . else . + {"INSTAGRAM_MEDIA_PUBLIC_BASE_URL": $instagram_public_base} end
    | if $instagram_lockbox == "" then . else . + {"INSTAGRAM_LOCKBOX_SECRET_ID": $instagram_lockbox} end
    | if $threads_enabled == "" then . else . + {"ENABLE_THREADS_PUBLISHING": $threads_enabled} end
    | if $threads_bucket == "" then . else . + {"THREADS_MEDIA_BUCKET": $threads_bucket} end
    | if $threads_public_base == "" then . else . + {"THREADS_MEDIA_PUBLIC_BASE_URL": $threads_public_base} end
    | if $threads_lockbox == "" then . else . + {"THREADS_LOCKBOX_SECRET_ID": $threads_lockbox} end
    | to_entries
    | sort_by(.key)
    | map([(.key + "=" + (.value | tostring))] | @csv)
    | join(",")
  ')"
  [[ -z "${environment_csv}" ]] || CREATE_ARGS+=(--environment "${environment_csv}")

  while IFS= read -r value; do
    [[ -z "${value}" ]] || CREATE_ARGS+=(--secret "${value}")
  done < <(printf '%s' "${version_json}" | "${JQ_BIN}" -r --arg liquipedia_override_id "${LIQUIPEDIA_SECRET_ID}" --arg proxy_override_id "${TELEGRAM_PROXY_SECRET_ID}" --arg token_override_id "${TELEGRAM_TOKEN_SECRET_ID}" --arg xray_override_id "${XRAY_SECRET_ID}" '
    (.secrets // [])[]
    | select($liquipedia_override_id == "" or .environment_variable != "LIQUIPEDIA_API_KEY")
    | select($proxy_override_id == "" or .environment_variable != "TELEGRAM_PROXY_URL")
    | select($token_override_id == "" or .environment_variable != "TELEGRAM_TOKEN")
    | select($xray_override_id == "" or .environment_variable != "XRAY_CONFIG_JSON")
    | "id=\(.id),version-id=\(.version_id),key=\(.key),environment-variable=\(.environment_variable)"
  ')

  if [[ -n "${LIQUIPEDIA_SECRET_ID}" ]]; then
    CREATE_ARGS+=(
      --secret
      "id=${LIQUIPEDIA_SECRET_ID},version-id=${LIQUIPEDIA_SECRET_VERSION_ID},key=${LIQUIPEDIA_SECRET_KEY},environment-variable=LIQUIPEDIA_API_KEY"
    )
  fi

  if [[ -n "${TELEGRAM_PROXY_SECRET_ID}" ]]; then
    CREATE_ARGS+=(
      --secret
      "id=${TELEGRAM_PROXY_SECRET_ID},version-id=${TELEGRAM_PROXY_SECRET_VERSION_ID},key=${TELEGRAM_PROXY_SECRET_KEY},environment-variable=TELEGRAM_PROXY_URL"
    )
  fi

  if [[ -n "${TELEGRAM_TOKEN_SECRET_ID}" ]]; then
    CREATE_ARGS+=(
      --secret
      "id=${TELEGRAM_TOKEN_SECRET_ID},version-id=${TELEGRAM_TOKEN_SECRET_VERSION_ID},key=${TELEGRAM_TOKEN_SECRET_KEY},environment-variable=TELEGRAM_TOKEN"
    )
  fi

  if [[ -n "${XRAY_SECRET_ID}" ]]; then
    CREATE_ARGS+=(
      --secret
      "id=${XRAY_SECRET_ID},version-id=${XRAY_SECRET_VERSION_ID},key=${XRAY_SECRET_KEY},environment-variable=XRAY_CONFIG_JSON"
    )
  fi

  if is_nonempty_json_value "${version_json}" '.metadata_options'; then
    value="$(printf '%s' "${version_json}" | "${JQ_BIN}" -r '
      .metadata_options
      | to_entries
      | sort_by(.key)
      | map("\(.key)=\(.value)")
      | join(",")
    ')"
    CREATE_ARGS+=(--metadata-options "${value}")
  fi

  if is_nonempty_json_value "${version_json}" '.log_options.disabled'; then
    CREATE_ARGS+=(--no-logging)
  else
    value="$(printf '%s' "${version_json}" | "${JQ_BIN}" -r '.log_options.log_group_id // empty')"
    if [[ -n "${value}" ]]; then
      CREATE_ARGS+=(--log-group-id "${value}")
    else
      value="$(printf '%s' "${version_json}" | "${JQ_BIN}" -r '.log_options.folder_id // empty')"
      [[ -z "${value}" ]] || CREATE_ARGS+=(--log-folder-id "${value}")
    fi
    value="$(printf '%s' "${version_json}" | "${JQ_BIN}" -r '.log_options.min_level // empty | ascii_downcase')"
    [[ -z "${value}" ]] || CREATE_ARGS+=(--min-log-level "${value}")
  fi
}

preflight() {
  local recovery="${1:-0}"
  local function_json
  local function_folder_id
  local triggers_json

  [[ -n "${FUNCTION_ID}" ]] || die "Set YC_FUNCTION_ID"
  [[ "${PRODUCTION_TAG}" != '$latest' ]] || die "YC_PRODUCTION_TAG must be a stable tag, not \$latest"
  [[ "${CANDIDATE_TAG}" != "${PRODUCTION_TAG}" ]] || die "Candidate and production tags must differ"
  [[ "${ROLLBACK_TAG}" != "${PRODUCTION_TAG}" && "${ROLLBACK_TAG}" != "${CANDIDATE_TAG}" ]] \
    || die "Production, candidate and rollback tags must differ"
  [[ "${CANDIDATE_TAG}" != '$latest' && "${ROLLBACK_TAG}" != '$latest' ]] \
    || die "Candidate and rollback tags must not be \$latest"

  require_command "${YC_BIN}"
  require_command "${JQ_BIN}"
  require_command "${PYTHON_BIN}"
  validate_upload_limit
  validate_positive_integer "YC_EXPECTED_TRIGGER_COUNT" "${EXPECTED_TRIGGER_COUNT}"
  validate_positive_integer "YC_PACKAGE_LIFECYCLE_MAX_DAYS" "${PACKAGE_LIFECYCLE_MAX_DAYS}"
  [[ "${SMOKE_LOG_WAIT_SECONDS}" =~ ^[0-9]+$ ]] || die "YC_SMOKE_LOG_WAIT_SECONDS must be a non-negative integer"
  (( SMOKE_LOG_WAIT_SECONDS <= 60 )) || die "YC_SMOKE_LOG_WAIT_SECONDS must be at most 60"
  validate_positive_integer "YC_SMOKE_INVOKE_TIMEOUT_SECONDS" "${SMOKE_INVOKE_TIMEOUT_SECONDS}"
  validate_positive_integer "YC_SMOKE_LOG_TIMEOUT_SECONDS" "${SMOKE_LOG_TIMEOUT_SECONDS}"
  printf '%s' "${DRY_RUN_PAYLOAD}" | "${JQ_BIN}" -e \
    'type == "object" and .dry_run == true and (has("messages") | not)' >/dev/null \
    || die "YC_DRY_RUN_PAYLOAD must be direct JSON with dry_run=true and no timer messages envelope"

  function_json="$("${YC_BIN}" serverless function get "${FUNCTION_ID}" --format json)"
  function_folder_id="$(jq_from "${function_json}" '.folder_id')"
  [[ "${function_folder_id}" == "${EXPECTED_FOLDER_ID}" ]] \
    || die "Function belongs to folder ${function_folder_id}, expected ${EXPECTED_FOLDER_ID}"

  PRODUCTION_JSON="$("${YC_BIN}" serverless function version get-by-tag \
    --function-id "${FUNCTION_ID}" \
    --tag "${PRODUCTION_TAG}" \
    --format json 2>/dev/null)" || die "Tag ${PRODUCTION_TAG} does not point to an active version"

  [[ "$(jq_from "${PRODUCTION_JSON}" '.status')" == "ACTIVE" ]] \
    || die "Production version is not ACTIVE"
  [[ "$(jq_from "${PRODUCTION_JSON}" '.function_id')" == "${FUNCTION_ID}" ]] \
    || die "Production tag points to another function"

  # Recovery must not be blocked by the very configuration it is reverting.
  [[ "${recovery}" != "1" ]] || return 0
  validate_required_configuration "${PRODUCTION_JSON}"
  validate_liquipedia_override
  validate_telegram_proxy_override
  validate_telegram_token_override
  validate_instagram_override
  validate_threads_override

  triggers_json="$("${YC_BIN}" serverless trigger list \
    --folder-id "${EXPECTED_FOLDER_ID}" \
    --format json)"
  validate_trigger_tags "${triggers_json}"
}

run_check() {
  preflight
  printf 'Preflight passed. Production version: %s; environment variables: %s; Lockbox bindings: %s.\n' \
    "$(jq_from "${PRODUCTION_JSON}" '.id')" \
    "$(printf '%s' "${PRODUCTION_JSON}" | "${JQ_BIN}" -r '(.environment // {}) | length')" \
    "$(printf '%s' "${PRODUCTION_JSON}" | "${JQ_BIN}" -r '(.secrets // []) | length')"
}

run_version_smoke() {
  local tag="$1"
  local version_id="$2"
  local started_at
  local checked_at
  local invoke_output=""
  local response_body=""
  local logs_output=""
  local status_code=""
  local dry_run_confirmed="false"
  local error_code=""
  local startup_error="false"

  SMOKE_SUMMARY='null'
  started_at="$(utc_now)"
  if ! verify_tag_points_to "${tag}" "${version_id}"; then
    error_code="tag_mismatch_before_smoke"
  elif ! invoke_output="$(run_with_timeout "${SMOKE_INVOKE_TIMEOUT_SECONDS}" "${YC_BIN}" serverless function invoke "${FUNCTION_ID}" \
    --tag "${tag}" \
    --data "${DRY_RUN_PAYLOAD}")"; then
    error_code="invoke_failed"
  else
    status_code="$(printf '%s' "${invoke_output}" | "${JQ_BIN}" -r '.statusCode // empty' 2>/dev/null || true)"
    if [[ "${status_code}" != "200" ]]; then
      error_code="non_200_response"
    elif ! response_body="$(printf '%s' "${invoke_output}" | "${JQ_BIN}" -cer '
      .body | if type == "string" then fromjson else . end
    ' 2>/dev/null)"; then
      error_code="invalid_response_body"
    elif ! printf '%s' "${response_body}" | "${JQ_BIN}" -e 'type == "object" and .dry_run == true' >/dev/null; then
      error_code="dry_run_not_confirmed"
    else
      dry_run_confirmed="true"
    fi
  fi

  if (( SMOKE_LOG_WAIT_SECONDS > 0 )); then
    sleep "${SMOKE_LOG_WAIT_SECONDS}"
  fi
  if ! logs_output="$(run_with_timeout "${SMOKE_LOG_TIMEOUT_SECONDS}" "${YC_BIN}" serverless function version logs "${version_id}" \
    --since "${started_at}" \
    --levels error,fatal --limit 1000 \
    --format json)"; then
    [[ -n "${error_code}" ]] || error_code="log_read_failed"
  elif ! printf '%s' "${logs_output}" | "${JQ_BIN}" -e . >/dev/null 2>&1; then
    [[ -n "${error_code}" ]] || error_code="invalid_log_response"
  elif printf '%s' "${logs_output}" | "${JQ_BIN}" -e '
    [.. | strings]
    | any(test("traceback|modulenotfounderror|importerror|syntaxerror|handler[^a-z]+not[^a-z]+found|initialization[^a-z]+error|failed[^a-z]+to[^a-z]+load|runtime[^a-z]+error"; "i"))
  ' >/dev/null; then
    startup_error="true"
    [[ -n "${error_code}" ]] || error_code="startup_error_in_logs"
  fi
  if ! verify_tag_points_to "${tag}" "${version_id}"; then
    error_code="tag_mismatch_after_smoke"
  fi

  checked_at="$(utc_now)"
  SMOKE_SUMMARY="$("${JQ_BIN}" -cn \
    --arg checked_at "${checked_at}" \
    --arg tag "${tag}" \
    --arg version_id "${version_id}" \
    --arg status_code "${status_code}" \
    --argjson dry_run "${dry_run_confirmed}" \
    --argjson startup_error "${startup_error}" \
    --arg error "${error_code}" '
      {
        checked_at: $checked_at,
        tag: $tag,
        version_id: $version_id,
        status_code: (try ($status_code | tonumber) catch null),
        dry_run_confirmed: $dry_run,
        startup_error_detected: $startup_error,
        error: (if $error == "" then null else $error end)
      }
    ')"

  [[ -z "${error_code}" ]]
}

create_release_manifest() {
  local manifest_path="$1"
  local candidate_id="$2"
  local previous_id="$3"
  local archive_path="$4"
  local archive_size="$5"
  local created_at="$6"
  local git_dirty="$7"

  mkdir -p "$(dirname "${manifest_path}")"
  "${JQ_BIN}" -n \
    --arg created_at "${created_at}" \
    --arg git_sha "${RELEASE_GIT_SHA}" \
    --argjson git_dirty "${git_dirty}" \
    --arg archive_path "${archive_path}" \
    --argjson archive_size "${archive_size}" \
    --arg archive_sha256 "${FUNCTION_PACKAGE_SHA256}" \
    --arg package_bucket "${FUNCTION_PACKAGE_BUCKET}" \
    --arg package_object "${FUNCTION_PACKAGE_OBJECT:-}" \
    --arg function_id "${FUNCTION_ID}" \
    --arg folder_id "${EXPECTED_FOLDER_ID}" \
    --arg previous_id "${previous_id}" \
    --arg candidate_id "${candidate_id}" \
    --arg production_tag "${PRODUCTION_TAG}" \
    --arg candidate_tag "${CANDIDATE_TAG}" \
    --arg rollback_tag "${ROLLBACK_TAG}" \
    --argjson smoke_payload "${DRY_RUN_PAYLOAD}" '
      {
        schema_version: 1,
        status: "candidate_created",
        created_at: $created_at,
        updated_at: $created_at,
        git_sha: $git_sha,
        git_dirty: $git_dirty,
        smoke_payload: $smoke_payload,
        archive_path: $archive_path,
        archive_size_bytes: $archive_size,
        archive_sha256: $archive_sha256,
        package_bucket: (if $package_bucket == "" then null else $package_bucket end),
        package_object: (if $package_object == "" then null else $package_object end),
        function_id: $function_id,
        folder_id: $folder_id,
        previous_version_id: $previous_id,
        candidate_version_id: $candidate_id,
        tags: {
          production: $production_tag,
          candidate: $candidate_tag,
          rollback: $rollback_tag
        },
        checks: {
          candidate_smoke: null,
          production_smoke: null,
          rollback_smoke: null
        },
        promoted_at: null,
        rolled_back_at: null
      }
    ' >"${manifest_path}"
}

use_manifest_bucket() {
  local manifest_path="$1"
  local manifest_bucket

  manifest_bucket="$("${JQ_BIN}" -r '.package_bucket // empty' "${manifest_path}")"
  if [[ -n "${FUNCTION_PACKAGE_BUCKET}" && -n "${manifest_bucket}" && "${FUNCTION_PACKAGE_BUCKET}" != "${manifest_bucket}" ]]; then
    die "YC_FUNCTION_PACKAGE_BUCKET does not match the release manifest"
  fi
  if [[ -z "${FUNCTION_PACKAGE_BUCKET}" ]]; then
    FUNCTION_PACKAGE_BUCKET="${manifest_bucket}"
  fi
}

run_candidate() {
  local zip_path
  local archive_size
  local create_output
  local candidate_id
  local previous_id
  local created_at
  local short_sha
  local git_dirty="false"
  local manifest_path

  require_command "${YC_BIN}"
  require_command "${JQ_BIN}"
  require_command git
  [[ -x "${BUILD_SCRIPT}" ]] || die "Build script is not executable: ${BUILD_SCRIPT}"
  validate_upload_limit

  RELEASE_GIT_SHA="$(git -C "${ROOT_DIR}" rev-parse --verify HEAD)" \
    || die "Cannot resolve release Git SHA"
  if [[ -n "$(git -C "${ROOT_DIR}" status --porcelain --untracked-files=normal)" ]]; then
    git_dirty="true"
    [[ "${YC_ALLOW_DIRTY_RELEASE:-}" == "1" ]] \
      || die "Release candidates require a clean Git working tree"
  fi

  zip_path="$(XRAY_ENABLED=1 "${BUILD_SCRIPT}")"
  [[ -f "${zip_path}" ]] || die "Build did not produce archive: ${zip_path}"
  archive_size="$(file_size_bytes "${zip_path}")"
  [[ "${archive_size}" =~ ^[1-9][0-9]*$ ]] || die "Build produced an empty or invalid archive"
  FUNCTION_PACKAGE_SHA256="$(sha256_file "${zip_path}")"
  [[ "$(git -C "${ROOT_DIR}" rev-parse HEAD)" == "${RELEASE_GIT_SHA}" ]] \
    || die "Git HEAD changed during the build"
  if [[ "${git_dirty}" == "false" ]]; then
    [[ -z "$(git -C "${ROOT_DIR}" status --porcelain --untracked-files=normal)" ]] \
      || die "Git working tree changed during the build"
  fi

  if (( archive_size > DIRECT_UPLOAD_MAX_BYTES )) && [[ -z "${FUNCTION_PACKAGE_BUCKET}" ]]; then
    die "Archive is ${archive_size} bytes; archives above ${DIRECT_UPLOAD_MAX_BYTES} bytes require a private YC_FUNCTION_PACKAGE_BUCKET"
  fi
  if [[ -n "${FUNCTION_PACKAGE_BUCKET}" ]]; then
    validate_positive_integer "YC_PACKAGE_LIFECYCLE_MAX_DAYS" "${PACKAGE_LIFECYCLE_MAX_DAYS}"
    validate_package_bucket
  fi

  preflight
  previous_id="$(jq_from "${PRODUCTION_JSON}" '.id')"
  short_sha="${RELEASE_GIT_SHA:0:12}"
  if [[ -n "${FUNCTION_PACKAGE_BUCKET}" ]]; then
    FUNCTION_PACKAGE_OBJECT="${PACKAGE_PREFIX}/${short_sha}/${FUNCTION_PACKAGE_SHA256}.zip"
    "${YC_BIN}" storage s3api put-object \
      --bucket "${FUNCTION_PACKAGE_BUCKET}" \
      --key "${FUNCTION_PACKAGE_OBJECT}" \
      --body "${zip_path}" \
      --acl private \
      --content-type application/zip >/dev/null
  else
    FUNCTION_PACKAGE_OBJECT=""
  fi

  build_create_arguments "${PRODUCTION_JSON}" "${zip_path}"
  create_output="$("${YC_BIN}" "${CREATE_ARGS[@]}")"
  candidate_id="$(jq_from "${create_output}" '.id')"
  [[ "$(jq_from "${create_output}" '.status')" == "ACTIVE" ]] \
    || die "Candidate version ${candidate_id} is not ACTIVE"

  created_at="$(utc_now)"
  manifest_path="${RELEASE_DIR}/${candidate_id}.json"
  create_release_manifest \
    "${manifest_path}" "${candidate_id}" "${previous_id}" "${zip_path}" \
    "${archive_size}" "${created_at}" "${git_dirty}"

  if run_version_smoke "${CANDIDATE_TAG}" "${candidate_id}"; then
    write_manifest "${manifest_path}" \
      '.status = "candidate_validated" | .updated_at = $now | .checks.candidate_smoke = $smoke' \
      --arg now "$(utc_now)" --argjson smoke "${SMOKE_SUMMARY}"
    upload_manifest "${manifest_path}"
    printf 'Candidate validated: %s. Production is unchanged. Manifest: %s\n' \
      "${candidate_id}" "${manifest_path}"
    return 0
  fi

  write_manifest "${manifest_path}" \
    '.status = "candidate_failed" | .updated_at = $now | .checks.candidate_smoke = $smoke' \
    --arg now "$(utc_now)" --argjson smoke "${SMOKE_SUMMARY}"
  upload_manifest "${manifest_path}"
  die "Candidate smoke failed; manifest: ${manifest_path}"
}

verify_tag_points_to() {
  local tag="$1"
  local expected_id="$2"
  local tagged_json

  tagged_json="$("${YC_BIN}" serverless function version get-by-tag \
    --function-id "${FUNCTION_ID}" \
    --tag "${tag}" \
    --format json 2>/dev/null)" || return 1
  [[ "$(jq_from "${tagged_json}" '.id')" == "${expected_id}" ]]
}

perform_rollback() {
  local manifest_path="$1"
  local candidate_id
  local previous_id
  local current_json
  local current_id
  local previous_json
  local trigger_json
  local rollback_status="rollback_failed"
  local rollback_smoke='null'
  local candidate_json
  local expected_description
  local git_sha
  local archive_sha
  local tag_restored="false"

  SMOKE_SUMMARY='null'
  candidate_id="$("${JQ_BIN}" -er '.candidate_version_id' "${manifest_path}")"
  previous_id="$("${JQ_BIN}" -er '.previous_version_id' "${manifest_path}")"
  candidate_json="$("${YC_BIN}" serverless function version get "${candidate_id}" --format json)"
  git_sha="$("${JQ_BIN}" -er '.git_sha' "${manifest_path}")"
  archive_sha="$("${JQ_BIN}" -er '.archive_sha256' "${manifest_path}")"
  expected_description="release git=${git_sha:0:12} sha256=${archive_sha} previous=${previous_id}"
  [[ "$(jq_from "${candidate_json}" '.function_id')" == "${FUNCTION_ID}" \
     && "$(jq_from "${candidate_json}" '.description')" == "${expected_description}" ]] \
    || die "Rollback refused: candidate metadata does not match manifest"
  current_json="$("${YC_BIN}" serverless function version get-by-tag \
    --function-id "${FUNCTION_ID}" --tag "${PRODUCTION_TAG}" --format json)"
  current_id="$(jq_from "${current_json}" '.id')"
  [[ "${current_id}" == "${candidate_id}" || "${current_id}" == "${previous_id}" ]] \
    || die "Rollback refused: production is ${current_id}, manifest candidate is ${candidate_id}"

  previous_json="$("${YC_BIN}" serverless function version get "${previous_id}" --format json)"
  [[ "$(jq_from "${previous_json}" '.status')" == "ACTIVE" ]] \
    || die "Rollback version ${previous_id} is not ACTIVE"
  [[ "$(jq_from "${previous_json}" '.function_id')" == "${FUNCTION_ID}" ]] \
    || die "Rollback version belongs to another function"

  write_manifest "${manifest_path}" \
    '.status = "rollback_pending" | .updated_at = $now' --arg now "$(utc_now)"
  # A failed CLI response can follow a successful tag move. Always read back.
  if [[ "${current_id}" == "${candidate_id}" ]]; then
    verify_tag_points_to "${PRODUCTION_TAG}" "${candidate_id}" \
      || die "Production changed before rollback; refusing to overwrite another release"
    "${YC_BIN}" serverless function version set-tag \
      --id "${previous_id}" --tag "${PRODUCTION_TAG}" --format json >/dev/null 2>&1 \
      || printf 'WARNING: rollback tag command failed; checking actual state.\n' >&2
  fi
  verify_tag_points_to "${PRODUCTION_TAG}" "${previous_id}" && tag_restored="true"
  if [[ "${tag_restored}" == "true" ]] \
    && trigger_json="$("${YC_BIN}" serverless trigger list --folder-id "${EXPECTED_FOLDER_ID}" --format json)" \
    && (validate_trigger_tags "${trigger_json}") \
    && run_version_smoke "${PRODUCTION_TAG}" "${previous_id}"; then
    rollback_status="rolled_back"
    rollback_smoke="${SMOKE_SUMMARY}"
  else
    [[ -z "${SMOKE_SUMMARY:-}" ]] || rollback_smoke="${SMOKE_SUMMARY}"
  fi

  write_manifest "${manifest_path}" \
    '.status = $status | .updated_at = $now | .rolled_back_at = (if $restored then $now else null end) | .checks.rollback_smoke = $smoke' \
    --arg status "${rollback_status}" --arg now "$(utc_now)" --argjson smoke "${rollback_smoke}" \
    --argjson restored "${tag_restored}"
  upload_manifest "${manifest_path}" \
    || die "Rollback status is ${rollback_status}; local manifest saved but remote copy failed"

  [[ "${rollback_status}" == "rolled_back" ]] \
    || die "Rollback could not be fully verified; inspect tag and manifest: ${manifest_path}"
  printf 'Rollback verified. Production version: %s. Manifest: %s\n' "${previous_id}" "${manifest_path}"
}

run_promote() {
  local manifest_path="$1"
  local manifest_status
  local previous_id
  local candidate_id
  local candidate_json
  local candidate_description
  local expected_description
  local manifest_git_sha
  local manifest_archive_sha
  local trigger_json
  local promotion_failed="false"
  local smoke_passed="false"

  [[ "${YC_PROMOTE_APPROVED:-}" == "1" ]] \
    || die "Set YC_PROMOTE_APPROVED=1 only after explicit production approval"
  preflight
  validate_manifest "${manifest_path}"
  use_manifest_bucket "${manifest_path}"
  [[ -z "${FUNCTION_PACKAGE_BUCKET}" ]] || validate_package_bucket

  manifest_status="$("${JQ_BIN}" -er '.status' "${manifest_path}")"
  [[ "${manifest_status}" == "candidate_validated" ]] \
    || die "Manifest status must be candidate_validated, got ${manifest_status}"
  "${JQ_BIN}" -e '
    .checks.candidate_smoke as $check
    | $check.version_id == .candidate_version_id
      and $check.status_code == 200
      and $check.dry_run_confirmed == true
      and $check.startup_error_detected == false
      and $check.error == null
  ' "${manifest_path}" >/dev/null || die "Manifest has no successful candidate smoke evidence"
  if [[ "$("${JQ_BIN}" -r '.git_dirty' "${manifest_path}")" == "true" ]]; then
    [[ "${YC_ALLOW_DIRTY_RELEASE:-}" == "1" ]] \
      || die "Refusing to promote a candidate built from a dirty Git working tree"
  fi
  previous_id="$("${JQ_BIN}" -er '.previous_version_id' "${manifest_path}")"
  candidate_id="$("${JQ_BIN}" -er '.candidate_version_id' "${manifest_path}")"
  [[ "$(jq_from "${PRODUCTION_JSON}" '.id')" == "${previous_id}" ]] \
    || die "Production changed after candidate creation; refusing stale promotion"

  candidate_json="$("${YC_BIN}" serverless function version get "${candidate_id}" --format json)"
  [[ "$(jq_from "${candidate_json}" '.status')" == "ACTIVE" ]] \
    || die "Candidate version ${candidate_id} is not ACTIVE"
  [[ "$(jq_from "${candidate_json}" '.function_id')" == "${FUNCTION_ID}" ]] \
    || die "Candidate version belongs to another function"
  manifest_git_sha="$("${JQ_BIN}" -er '.git_sha' "${manifest_path}")"
  manifest_archive_sha="$("${JQ_BIN}" -er '.archive_sha256' "${manifest_path}")"
  expected_description="release git=${manifest_git_sha:0:12} sha256=${manifest_archive_sha} previous=${previous_id}"
  candidate_description="$(printf '%s' "${candidate_json}" | "${JQ_BIN}" -r '.description // empty')"
  [[ "${candidate_description}" == "${expected_description}" ]] \
    || die "Candidate metadata does not match the release manifest"
  verify_tag_points_to "${CANDIDATE_TAG}" "${candidate_id}" \
    || die "Candidate tag no longer points to manifest version ${candidate_id}"

  "${YC_BIN}" serverless function version set-tag \
    --id "${previous_id}" --tag "${ROLLBACK_TAG}" --format json >/dev/null
  verify_tag_points_to "${ROLLBACK_TAG}" "${previous_id}" \
    || die "Rollback tag verification failed; production was not changed"

  # Persist intent before moving production, so a lost response is recoverable.
  write_manifest "${manifest_path}" \
    '.status = "promotion_pending_smoke" | .updated_at = $now | .promoted_at = $now' \
    --arg now "$(utc_now)"
  upload_manifest "${manifest_path}"
  verify_tag_points_to "${PRODUCTION_TAG}" "${previous_id}" \
    || die "Production changed before promotion; refusing to overwrite another release"
  "${YC_BIN}" serverless function version set-tag \
    --id "${candidate_id}" --tag "${PRODUCTION_TAG}" --format json >/dev/null 2>&1 \
    || printf 'WARNING: promotion tag command failed; checking actual state.\n' >&2

  if ! write_manifest "${manifest_path}" \
    '.status = "promotion_pending_smoke" | .updated_at = $now | .promoted_at = $now' \
    --arg now "$(utc_now)"; then
    promotion_failed="true"
  elif ! upload_manifest "${manifest_path}"; then
    promotion_failed="true"
  fi

  verify_tag_points_to "${PRODUCTION_TAG}" "${candidate_id}" || promotion_failed="true"
  if [[ "${promotion_failed}" == "false" ]]; then
    if trigger_json="$("${YC_BIN}" serverless trigger list --folder-id "${EXPECTED_FOLDER_ID}" --format json)"; then
      (validate_trigger_tags "${trigger_json}") || promotion_failed="true"
    else
      promotion_failed="true"
    fi
  fi
  if [[ "${promotion_failed}" == "false" ]]; then
    if run_version_smoke "${PRODUCTION_TAG}" "${candidate_id}"; then
      smoke_passed="true"
    else
      promotion_failed="true"
    fi
  fi
  if [[ "${smoke_passed}" == "true" ]]; then
    if write_manifest "${manifest_path}" \
      '.status = "promoted" | .updated_at = $now | .checks.production_smoke = $smoke' \
      --arg now "$(utc_now)" --argjson smoke "${SMOKE_SUMMARY}" \
      && upload_manifest "${manifest_path}"; then
      printf 'Promotion verified. Previous version: %s; production version: %s. Manifest: %s\n' \
        "${previous_id}" "${candidate_id}" "${manifest_path}"
      return 0
    fi
    promotion_failed="true"
  fi

  if [[ -n "${SMOKE_SUMMARY:-}" ]]; then
    if write_manifest "${manifest_path}" \
      '.status = "promotion_failed" | .updated_at = $now | .checks.production_smoke = $smoke' \
      --arg now "$(utc_now)" --argjson smoke "${SMOKE_SUMMARY}"; then
      upload_manifest "${manifest_path}" || printf 'WARNING: failed to upload promotion failure manifest.\n' >&2
    fi
  else
    if write_manifest "${manifest_path}" \
      '.status = "promotion_failed" | .updated_at = $now' \
      --arg now "$(utc_now)"; then
      upload_manifest "${manifest_path}" || printf 'WARNING: failed to upload promotion failure manifest.\n' >&2
    fi
  fi
  printf 'Promotion smoke failed; starting automatic rollback.\n' >&2
  perform_rollback "${manifest_path}"
  die "Promotion failed and was rolled back"
}

run_smoke() {
  local manifest_path="$1"
  local candidate_id
  local trigger_json

  preflight
  validate_manifest "${manifest_path}"
  use_manifest_bucket "${manifest_path}"
  candidate_id="$("${JQ_BIN}" -er '.candidate_version_id' "${manifest_path}")"
  verify_tag_points_to "${PRODUCTION_TAG}" "${candidate_id}" \
    || die "Production does not point to manifest candidate ${candidate_id}"
  trigger_json="$("${YC_BIN}" serverless trigger list --folder-id "${EXPECTED_FOLDER_ID}" --format json)"
  validate_trigger_tags "${trigger_json}"
  if run_version_smoke "${PRODUCTION_TAG}" "${candidate_id}"; then
    write_manifest "${manifest_path}" \
      '.updated_at = $now | .checks.production_smoke = $smoke' \
      --arg now "$(utc_now)" --argjson smoke "${SMOKE_SUMMARY}"
    upload_manifest "${manifest_path}"
    printf 'Production smoke passed for version %s.\n' "${candidate_id}"
    return 0
  fi
  write_manifest "${manifest_path}" \
    '.updated_at = $now | .checks.production_smoke = $smoke' \
    --arg now "$(utc_now)" --argjson smoke "${SMOKE_SUMMARY}"
  upload_manifest "${manifest_path}"
  die "Production smoke failed for version ${candidate_id}"
}

run_rollback() {
  local manifest_path="$1"

  [[ "${YC_ROLLBACK_APPROVED:-}" == "1" ]] \
    || die "Set YC_ROLLBACK_APPROVED=1 only after explicit rollback approval"
  preflight 1
  validate_manifest "${manifest_path}"
  "${JQ_BIN}" -e '.promoted_at != null' "${manifest_path}" >/dev/null \
    || die "Manifest does not record a promotion attempt"
  use_manifest_bucket "${manifest_path}"
  perform_rollback "${manifest_path}"
}

main() {
  case "${1:-}" in
    check)
      [[ "$#" -eq 1 ]] || die "check does not accept extra arguments"
      run_check
      ;;
    candidate)
      [[ "$#" -eq 1 ]] || die "candidate does not accept extra arguments"
      acquire_release_lock
      run_candidate
      ;;
    promote)
      [[ "$#" -eq 2 ]] || die "promote requires exactly one release manifest"
      acquire_release_lock
      run_promote "$2"
      ;;
    smoke)
      [[ "$#" -eq 2 ]] || die "smoke requires exactly one release manifest"
      acquire_release_lock
      run_smoke "$2"
      ;;
    rollback)
      [[ "$#" -eq 2 ]] || die "rollback requires exactly one release manifest"
      acquire_release_lock
      run_rollback "$2"
      ;;
    deploy)
      die "deploy was removed: run candidate, review its manifest, then run promote"
      ;;
    -h|--help|help)
      usage
      ;;
    *)
      usage >&2
      exit 2
      ;;
  esac
}

main "$@"

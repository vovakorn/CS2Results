#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
YC_BIN="${YC_BIN:-yc}"
JQ_BIN="${JQ_BIN:-jq}"
BUILD_SCRIPT="${YC_BUILD_SCRIPT:-${ROOT_DIR}/scripts/build_function_zip.sh}"

FUNCTION_ID="${YC_FUNCTION_ID:-}"
EXPECTED_FOLDER_ID="${YC_FOLDER_ID:-b1g5j8hk4gjas2vpvgqr}"
PRODUCTION_TAG="${YC_PRODUCTION_TAG:-production}"
CANDIDATE_TAG="${YC_CANDIDATE_TAG:-candidate}"
DRY_RUN_PAYLOAD="${YC_DRY_RUN_PAYLOAD:-{\"limit\":1,\"dry_run\":true}}"
LIQUIPEDIA_SECRET_ID="${YC_LIQUIPEDIA_SECRET_ID:-}"
LIQUIPEDIA_SECRET_VERSION_ID="${YC_LIQUIPEDIA_SECRET_VERSION_ID:-}"
LIQUIPEDIA_SECRET_KEY="${YC_LIQUIPEDIA_SECRET_KEY:-LIQUIPEDIA_API_KEY}"
LIQUIPEDIA_SHADOW_OVERRIDE="${YC_ENABLE_LIQUIPEDIA_SHADOW:-}"
TARGET_EXECUTION_TIMEOUT="${YC_EXECUTION_TIMEOUT:-120s}"

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
  YC_FUNCTION_ID=<id> YC_DEPLOY_APPROVED=1 scripts/deploy_yandex_function.sh deploy

Commands:
  check   Read-only validation of the production version and trigger tags.
  deploy  Build, create a candidate, run a dry-run, then atomically move the
          production tag. Requires YC_DEPLOY_APPROVED=1.

The script never reads Lockbox values. It copies only secret references from the
version currently tagged "production". All triggers invoking this function must
also use that tag; otherwise the script stops before creating a version.

For the first Liquipedia deployment, pass YC_LIQUIPEDIA_SECRET_ID,
YC_LIQUIPEDIA_SECRET_VERSION_ID, YC_LIQUIPEDIA_SECRET_KEY and
YC_ENABLE_LIQUIPEDIA_SHADOW=1. Only Lockbox references are passed; the API key
value is never read by this script.
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
       ($environment.ENABLE_LIQUIPEDIA_SHADOW // "0")]
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

  if [[ "${LIQUIPEDIA_SHADOW_OVERRIDE}" == "1" && -z "${LIQUIPEDIA_SECRET_ID}" ]]; then
    printf '%s' "${PRODUCTION_JSON}" | "${JQ_BIN}" -e \
      'any((.secrets // [])[]; .environment_variable == "LIQUIPEDIA_API_KEY")' >/dev/null \
      || die "Enabling Liquipedia shadow requires a pinned LIQUIPEDIA_API_KEY Lockbox reference"
  fi
}

validate_trigger_tags() {
  local triggers_json="$1"
  local bad_triggers
  local matching_count

  matching_count="$(printf '%s' "${triggers_json}" | "${JQ_BIN}" -r --arg function_id "${FUNCTION_ID}" '
    [ .[] | select(any(.rule | .. | objects; .function_id? == $function_id)) ] | length
  ')"
  [[ "${matching_count}" -gt 0 ]] || die "No triggers invoking function ${FUNCTION_ID} were found"

  bad_triggers="$(printf '%s' "${triggers_json}" | "${JQ_BIN}" -r \
    --arg function_id "${FUNCTION_ID}" \
    --arg production_tag "${PRODUCTION_TAG}" '
      .[]
      | select(any(.rule | .. | objects; .function_id? == $function_id and ((.function_tag? // "") != $production_tag)))
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
    --source-path "${zip_path}"
    --tags "${CANDIDATE_TAG}"
    --description "Safe deploy from production version $(jq_from "${version_json}" '.id')"
    --format json
  )

  memory_bytes="$(jq_from "${version_json}" '.resources.memory')"
  [[ "${memory_bytes}" =~ ^[0-9]+$ ]] || die "Production memory value is invalid"
  (( memory_bytes % 1048576 == 0 )) || die "Production memory is not a whole number of MiB"
  CREATE_ARGS+=(--memory "$((memory_bytes / 1048576))MB")

  value="$(jq_from "${version_json}" '.service_account_id')"
  CREATE_ARGS+=(--service-account-id "${value}")

  value="$(printf '%s' "${version_json}" | "${JQ_BIN}" -r '.concurrency // empty')"
  [[ -z "${value}" ]] || CREATE_ARGS+=(--concurrency "${value}")

  environment_csv="$(printf '%s' "${version_json}" | "${JQ_BIN}" -r --arg shadow "${LIQUIPEDIA_SHADOW_OVERRIDE}" '
    (.environment // {})
    | if $shadow == "" then . else . + {"ENABLE_LIQUIPEDIA_SHADOW": $shadow} end
    | to_entries
    | sort_by(.key)
    | map([(.key + "=" + (.value | tostring))] | @csv)
    | join(",")
  ')"
  [[ -z "${environment_csv}" ]] || CREATE_ARGS+=(--environment "${environment_csv}")

  while IFS= read -r value; do
    [[ -z "${value}" ]] || CREATE_ARGS+=(--secret "${value}")
  done < <(printf '%s' "${version_json}" | "${JQ_BIN}" -r --arg override_id "${LIQUIPEDIA_SECRET_ID}" '
    (.secrets // [])[]
    | select($override_id == "" or .environment_variable != "LIQUIPEDIA_API_KEY")
    | "id=\(.id),version-id=\(.version_id),key=\(.key),environment-variable=\(.environment_variable)"
  ')

  if [[ -n "${LIQUIPEDIA_SECRET_ID}" ]]; then
    CREATE_ARGS+=(
      --secret
      "id=${LIQUIPEDIA_SECRET_ID},version-id=${LIQUIPEDIA_SECRET_VERSION_ID},key=${LIQUIPEDIA_SECRET_KEY},environment-variable=LIQUIPEDIA_API_KEY"
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
  local function_json
  local function_folder_id
  local triggers_json

  [[ -n "${FUNCTION_ID}" ]] || die "Set YC_FUNCTION_ID"
  [[ "${PRODUCTION_TAG}" != '$latest' ]] || die "YC_PRODUCTION_TAG must be a stable tag, not \$latest"
  [[ "${CANDIDATE_TAG}" != "${PRODUCTION_TAG}" ]] || die "Candidate and production tags must differ"

  require_command "${YC_BIN}"
  require_command "${JQ_BIN}"
  [[ -x "${BUILD_SCRIPT}" ]] || die "Build script is not executable: ${BUILD_SCRIPT}"
  printf '%s' "${DRY_RUN_PAYLOAD}" | "${JQ_BIN}" -e \
    'type == "object" and .dry_run == true' >/dev/null \
    || die "YC_DRY_RUN_PAYLOAD must be valid JSON with dry_run=true"

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

  validate_required_configuration "${PRODUCTION_JSON}"
  validate_liquipedia_override

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

run_deploy() {
  local zip_path
  local create_output
  local candidate_id
  local invoke_output
  local response_body
  local promoted_json
  local promoted_id

  [[ "${YC_DEPLOY_APPROVED:-}" == "1" ]] \
    || die "Set YC_DEPLOY_APPROVED=1 only after explicit production approval"

  preflight
  zip_path="$("${BUILD_SCRIPT}")"
  [[ -f "${zip_path}" ]] || die "Build did not produce archive: ${zip_path}"

  build_create_arguments "${PRODUCTION_JSON}" "${zip_path}"
  create_output="$("${YC_BIN}" "${CREATE_ARGS[@]}")"
  candidate_id="$(jq_from "${create_output}" '.id')"
  [[ "$(jq_from "${create_output}" '.status')" == "ACTIVE" ]] \
    || die "Candidate version ${candidate_id} is not ACTIVE"
  printf 'Candidate created: %s. Production tag is unchanged.\n' "${candidate_id}"

  invoke_output="$("${YC_BIN}" serverless function invoke "${FUNCTION_ID}" \
    --tag "${CANDIDATE_TAG}" \
    --data "${DRY_RUN_PAYLOAD}")"
  [[ "$(jq_from "${invoke_output}" '.statusCode')" == "200" ]] \
    || die "Candidate dry-run returned a non-200 status"

  response_body="$(printf '%s' "${invoke_output}" | "${JQ_BIN}" -cer '
    .body | if type == "string" then fromjson else . end
  ')" || die "Candidate dry-run returned an invalid body"
  [[ "$(jq_from "${response_body}" '.dry_run')" == "true" ]] \
    || die "Candidate response does not confirm dry_run=true"

  "${YC_BIN}" serverless function version set-tag \
    --id "${candidate_id}" \
    --tag "${PRODUCTION_TAG}" \
    --format json >/dev/null

  promoted_json="$("${YC_BIN}" serverless function version get-by-tag \
    --function-id "${FUNCTION_ID}" \
    --tag "${PRODUCTION_TAG}" \
    --format json)"
  promoted_id="$(jq_from "${promoted_json}" '.id')"
  [[ "${promoted_id}" == "${candidate_id}" ]] || die "Production tag verification failed"

  printf 'Deploy complete. Previous version: %s; production version: %s; dry-run: passed.\n' \
    "$(jq_from "${PRODUCTION_JSON}" '.id')" "${candidate_id}"
}

main() {
  case "${1:-}" in
    check)
      [[ "$#" -eq 1 ]] || die "check does not accept extra arguments"
      run_check
      ;;
    deploy)
      [[ "$#" -eq 1 ]] || die "deploy does not accept extra arguments"
      run_deploy
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

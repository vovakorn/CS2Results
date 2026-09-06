from __future__ import annotations

import csv
import json
import os
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
DEPLOY_SCRIPT = ROOT / "scripts" / "deploy_yandex_function.sh"
FUNCTION_ID = "function-id"
FOLDER_ID = "folder-id"


BASE_VERSION = {
    "id": "production-version",
    "function_id": FUNCTION_ID,
    "runtime": "python312",
    "entrypoint": "cs2bot.main.handler",
    "resources": {"memory": "268435456"},
    "execution_timeout": "60s",
    "service_account_id": "service-account-id",
    "status": "ACTIVE",
    "concurrency": "1",
    "environment": {
        "ALERT_COOLDOWN_SECONDS": "21600",
        "BOT_MODE": "production",
        "DELIVERY_CLAIM_TTL_SECONDS": "300",
        "DISPLAY_TIMEZONE": "Europe/Moscow",
        "ENABLE_LIQUIPEDIA_FALLBACK": "0",
        "MATCH_SOURCE": "auto",
        "MAX_SOURCE_FUTURE_SKEW_HOURS": "6",
        "MAX_SOURCE_RESPONSE_BYTES": "5000000",
        "MAX_SOURCE_STALENESS_HOURS": "48",
        "OBJECT_STORAGE_BUCKET": "state-bucket",
        "OBJECT_STORAGE_ENDPOINT": "https://storage.yandexcloud.net",
        "REQUEST_TIMEOUT_SECONDS": "15",
        "TELEGRAM_ADMIN_CHAT_ID": "123456",
        "TELEGRAM_CHAT_ID": "@cs2_results",
        "TELEGRAM_MEDIA_CARDS": "1",
        "TELEGRAM_SPOILERS": "1",
        "VALUE_WITH_COMMA": "one,two",
    },
    "secrets": [
        {
            "environment_variable": name,
            "id": "lockbox-id",
            "version_id": "lockbox-version-id",
            "key": name,
        }
        for name in (
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "PANDASCORE_API_TOKEN",
            "TELEGRAM_TOKEN",
        )
    ],
    "log_options": {"folder_id": FOLDER_ID, "min_level": "INFO"},
    "metadata_options": {},
}


def _triggers(tag: str | None = "production", count: int = 5) -> list[dict[str, object]]:
    triggers = []
    for index in range(count):
        function_target: dict[str, str] = {"function_id": FUNCTION_ID}
        if tag is not None:
            function_target["function_tag"] = tag
        triggers.append(
            {
                "name": f"timer-{index}",
                "rule": {"timer": {"invoke_function_with_retry": function_target}},
            }
        )
    return triggers


@pytest.fixture
def fake_cloud(tmp_path: Path) -> dict[str, str]:
    yc_path = tmp_path / "yc"
    build_path = tmp_path / "build.sh"
    zip_path = tmp_path / "function.zip"
    log_path = tmp_path / "yc.log"
    state_path = tmp_path / "state.json"
    build_count_path = tmp_path / "build-count"
    release_dir = tmp_path / "releases"
    state_path.write_text(json.dumps({"production": "production-version"}), encoding="utf-8")

    yc_path.write_text(
        """#!/usr/bin/env python3
import json
import os
import pathlib
import sys

args = sys.argv[1:]
log_path = pathlib.Path(os.environ["FAKE_YC_LOG"])
with log_path.open("a", encoding="utf-8") as log:
    log.write(json.dumps(args) + "\\n")

base = json.loads(os.environ["FAKE_BASE_VERSION"])
state_path = pathlib.Path(os.environ["FAKE_STATE_PATH"])
state = json.loads(state_path.read_text(encoding="utf-8"))

def save_state():
    state_path.write_text(json.dumps(state), encoding="utf-8")

def version(version_id):
    result = dict(base, id=version_id, status="ACTIVE")
    description = state.get("descriptions", {}).get(version_id)
    if description is not None:
        result["description"] = description
    return result

if args[:3] == ["serverless", "function", "get"]:
    print(json.dumps({"id": os.environ["YC_FUNCTION_ID"], "folder_id": os.environ["YC_FOLDER_ID"]}))
elif args[:4] == ["serverless", "function", "version", "get-by-tag"]:
    tag = args[args.index("--tag") + 1]
    if tag not in state:
        raise SystemExit(1)
    print(json.dumps(version(state[tag])))
elif args[:4] == ["serverless", "function", "version", "get"]:
    print(json.dumps(version(args[4])))
elif args[:3] == ["serverless", "trigger", "list"]:
    print(os.environ["FAKE_TRIGGERS"])
elif args[:4] == ["serverless", "function", "version", "create"]:
    state["candidate"] = "candidate-version"
    state.setdefault("descriptions", {})["candidate-version"] = args[args.index("--description") + 1]
    save_state()
    print(json.dumps(version("candidate-version")))
elif args[:4] == ["serverless", "function", "version", "set-tag"]:
    tag = args[args.index("--tag") + 1]
    version_id = args[args.index("--id") + 1]
    state[tag] = version_id
    save_state()
    if tag == "production" and os.environ.get("FAKE_TAG_RESPONSE_LOST") == "1":
        raise SystemExit(1)
    print(json.dumps(version(version_id)))
elif args[:4] == ["serverless", "function", "version", "logs"]:
    print(os.environ.get("FAKE_VERSION_LOGS", "[]"))
elif args[:3] == ["serverless", "function", "invoke"]:
    tag = args[args.index("--tag") + 1]
    version_id = state[tag]
    should_fail = (
        os.environ.get("FAKE_FAIL_NEW_PRODUCTION_SMOKE") == "1"
        and tag == "production"
        and version_id == "candidate-version"
    )
    status_code = 500 if should_fail else 200
    if os.environ.get("FAKE_CHANGE_CANDIDATE_DURING_SMOKE") == "1" and tag == "candidate":
        state[tag] = "other-candidate"
        save_state()
    dry_run = "true" if os.environ.get("FAKE_STRING_DRY_RUN") else True
    print(json.dumps({"statusCode": status_code, "body": json.dumps({"dry_run": dry_run, "messages_sent": 0})}))
elif args[:3] == ["storage", "bucket", "get"]:
    public = os.environ.get("FAKE_BUCKET_PUBLIC") == "1"
    lifecycle = os.environ.get("FAKE_BUCKET_LIFECYCLE", "1") == "1"
    print(json.dumps({
        "name": args[3],
        "folder_id": os.environ["YC_FOLDER_ID"],
        "anonymous_access_flags": {"read": public, "list": False, "config_read": False},
        "acl": {"grants": []},
        "policy": json.loads(os.environ.get("FAKE_BUCKET_POLICY", "null")),
        "lifecycle_rules": ([{
            "id": "delete-old-function-packages",
            "enabled": True,
            "filter": {"prefix": "function-packages/"},
            "expiration": {"days": "30"},
        }] if lifecycle else []),
    }))
elif args[:3] == ["storage", "s3api", "put-object"]:
    if os.environ.get("FAKE_FAIL_MANIFEST_AFTER_PROMOTE") == "1" and state["production"] == "candidate-version":
        raise SystemExit(1)
    print(json.dumps({"etag": "fake"}))
else:
    print("unsupported fake yc call: " + repr(args), file=sys.stderr)
    raise SystemExit(2)
""",
        encoding="utf-8",
    )
    yc_path.chmod(0o755)

    build_path.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
printf 'archive-bytes-for-tests' >"${FAKE_ZIP_PATH}"
printf x >>"${FAKE_BUILD_COUNT_PATH}"
printf '%s\\n' "${FAKE_ZIP_PATH}"
""",
        encoding="utf-8",
    )
    build_path.chmod(0o755)

    return {
        **{key: value for key, value in os.environ.items() if not key.startswith(("YC_", "FAKE_"))},
        "YC_BIN": str(yc_path),
        "JQ_BIN": "/usr/bin/jq",
        "YC_BUILD_SCRIPT": str(build_path),
        "YC_FUNCTION_ID": FUNCTION_ID,
        "YC_FOLDER_ID": FOLDER_ID,
        "YC_RELEASE_DIR": str(release_dir),
        "YC_ALLOW_DIRTY_RELEASE": "1",
        "YC_SMOKE_LOG_WAIT_SECONDS": "0",
        "FAKE_BASE_VERSION": json.dumps(BASE_VERSION),
        "FAKE_TRIGGERS": json.dumps(_triggers()),
        "FAKE_YC_LOG": str(log_path),
        "FAKE_STATE_PATH": str(state_path),
        "FAKE_ZIP_PATH": str(zip_path),
        "FAKE_BUILD_COUNT_PATH": str(build_count_path),
    }


def _run(command: str | list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    args = [command] if isinstance(command, str) else command
    return subprocess.run(
        [str(DEPLOY_SCRIPT), *args],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _calls(env: dict[str, str]) -> list[list[str]]:
    path = Path(env["FAKE_YC_LOG"])
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _manifest_path(env: dict[str, str]) -> Path:
    manifests = list(Path(env["YC_RELEASE_DIR"]).glob("*.json"))
    assert len(manifests) == 1
    return manifests[0]


def _create_candidate(env: dict[str, str]) -> Path:
    result = _run("candidate", env)
    assert result.returncode == 0, result.stderr
    return _manifest_path(env)


def test_check_is_read_only(fake_cloud: dict[str, str]) -> None:
    result = _run("check", fake_cloud)

    assert result.returncode == 0, result.stderr
    assert "Preflight passed" in result.stdout
    assert not any(call[:4] == ["serverless", "function", "version", "create"] for call in _calls(fake_cloud))


def test_check_requires_exactly_five_timer_triggers(fake_cloud: dict[str, str]) -> None:
    fake_cloud["FAKE_TRIGGERS"] = json.dumps(_triggers(count=4))

    result = _run("check", fake_cloud)

    assert result.returncode != 0
    assert "Expected 5 timer triggers" in result.stderr


def test_check_rejects_trigger_without_production_tag(fake_cloud: dict[str, str]) -> None:
    fake_cloud["FAKE_TRIGGERS"] = json.dumps(_triggers(tag=None))

    result = _run("check", fake_cloud)

    assert result.returncode != 0
    assert "not pinned to tag production" in result.stderr


def test_large_archive_requires_bucket_before_cloud_functions_calls(fake_cloud: dict[str, str]) -> None:
    fake_cloud["YC_DIRECT_UPLOAD_MAX_BYTES"] = "8"

    result = _run("candidate", fake_cloud)

    assert result.returncode != 0
    assert "require a private YC_FUNCTION_PACKAGE_BUCKET" in result.stderr
    assert not any(call[:2] == ["serverless", "function"] for call in _calls(fake_cloud))


def test_candidate_rejects_public_package_bucket(fake_cloud: dict[str, str]) -> None:
    fake_cloud.update(
        {
            "YC_DIRECT_UPLOAD_MAX_BYTES": "8",
            "YC_FUNCTION_PACKAGE_BUCKET": "package-bucket",
            "FAKE_BUCKET_PUBLIC": "1",
        }
    )

    result = _run("candidate", fake_cloud)

    assert result.returncode != 0
    assert "anonymous access enabled" in result.stderr
    assert not any(call[:2] == ["serverless", "function"] for call in _calls(fake_cloud))


def test_candidate_requires_package_lifecycle(fake_cloud: dict[str, str]) -> None:
    fake_cloud.update(
        {
            "YC_DIRECT_UPLOAD_MAX_BYTES": "8",
            "YC_FUNCTION_PACKAGE_BUCKET": "package-bucket",
            "FAKE_BUCKET_LIFECYCLE": "0",
        }
    )

    result = _run("candidate", fake_cloud)

    assert result.returncode != 0
    assert "needs an enabled function-packages/ expiration lifecycle" in result.stderr


def test_candidate_preserves_configuration_and_writes_manifest(fake_cloud: dict[str, str]) -> None:
    fake_cloud.update(
        {
            "YC_DIRECT_UPLOAD_MAX_BYTES": "8",
            "YC_FUNCTION_PACKAGE_BUCKET": "package-bucket",
        }
    )

    manifest_path = _create_candidate(fake_cloud)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    calls = _calls(fake_cloud)
    create_call = next(call for call in calls if call[:4] == ["serverless", "function", "version", "create"])
    environment_csv = create_call[create_call.index("--environment") + 1]
    environment = dict(item.split("=", 1) for item in next(csv.reader([environment_csv])))

    assert environment == BASE_VERSION["environment"]
    assert create_call[create_call.index("--memory") + 1] == "256MB"
    assert create_call[create_call.index("--execution-timeout") + 1] == "120s"
    assert create_call[create_call.index("--service-account-id") + 1] == "service-account-id"
    assert create_call[create_call.index("--min-log-level") + 1] == "info"
    assert "--package-bucket-name" in create_call
    assert "--source-path" not in create_call

    secret_flags = [create_call[index + 1] for index, value in enumerate(create_call) if value == "--secret"]
    assert len(secret_flags) == 4
    assert all("id=lockbox-id" in value for value in secret_flags)
    assert all("version-id=lockbox-version-id" in value for value in secret_flags)

    assert manifest["status"] == "candidate_validated"
    assert manifest["previous_version_id"] == "production-version"
    assert manifest["candidate_version_id"] == "candidate-version"
    assert manifest["archive_size_bytes"] > 8
    assert len(manifest["archive_sha256"]) == 64
    assert manifest["package_bucket"] == "package-bucket"
    assert manifest["package_object"].startswith("function-packages/")
    assert manifest["checks"]["candidate_smoke"]["dry_run_confirmed"] is True


def test_promote_uses_validated_candidate_without_rebuilding(fake_cloud: dict[str, str]) -> None:
    manifest_path = _create_candidate(fake_cloud)
    fake_cloud["YC_PROMOTE_APPROVED"] = "1"

    result = _run(["promote", str(manifest_path)], fake_cloud)

    assert result.returncode == 0, result.stderr
    assert "Promotion verified" in result.stdout
    assert Path(fake_cloud["FAKE_BUILD_COUNT_PATH"]).read_text(encoding="utf-8") == "x"
    calls = _calls(fake_cloud)
    assert sum(call[:4] == ["serverless", "function", "version", "create"] for call in calls) == 1
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "promoted"
    assert manifest["checks"]["production_smoke"]["dry_run_confirmed"] is True
    state = json.loads(Path(fake_cloud["FAKE_STATE_PATH"]).read_text(encoding="utf-8"))
    assert state["production"] == "candidate-version"
    assert state["rollback"] == "production-version"


def test_promote_requires_explicit_approval(fake_cloud: dict[str, str]) -> None:
    manifest_path = _create_candidate(fake_cloud)
    call_count = len(_calls(fake_cloud))

    result = _run(["promote", str(manifest_path)], fake_cloud)

    assert result.returncode != 0
    assert "YC_PROMOTE_APPROVED=1" in result.stderr
    assert len(_calls(fake_cloud)) == call_count


def test_promote_rejects_stale_manifest(fake_cloud: dict[str, str]) -> None:
    manifest_path = _create_candidate(fake_cloud)
    state_path = Path(fake_cloud["FAKE_STATE_PATH"])
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["production"] = "other-production-version"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    fake_cloud["YC_PROMOTE_APPROVED"] = "1"

    result = _run(["promote", str(manifest_path)], fake_cloud)

    assert result.returncode != 0
    assert "Production changed after candidate creation" in result.stderr


def test_promote_rejects_manifest_that_does_not_match_candidate_metadata(
    fake_cloud: dict[str, str],
) -> None:
    manifest_path = _create_candidate(fake_cloud)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["archive_sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    fake_cloud["YC_PROMOTE_APPROVED"] = "1"

    result = _run(["promote", str(manifest_path)], fake_cloud)

    assert result.returncode != 0
    assert "metadata does not match" in result.stderr
    state = json.loads(Path(fake_cloud["FAKE_STATE_PATH"]).read_text(encoding="utf-8"))
    assert state["production"] == "production-version"


def test_failed_production_smoke_rolls_back_and_records_result(fake_cloud: dict[str, str]) -> None:
    manifest_path = _create_candidate(fake_cloud)
    fake_cloud.update({"YC_PROMOTE_APPROVED": "1", "FAKE_FAIL_NEW_PRODUCTION_SMOKE": "1"})

    result = _run(["promote", str(manifest_path)], fake_cloud)

    assert result.returncode != 0
    assert "was rolled back" in result.stderr
    state = json.loads(Path(fake_cloud["FAKE_STATE_PATH"]).read_text(encoding="utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert state["production"] == "production-version"
    assert manifest["status"] == "rolled_back"
    assert manifest["checks"]["production_smoke"]["status_code"] == 500
    assert manifest["checks"]["rollback_smoke"]["dry_run_confirmed"] is True


def test_manual_rollback_is_verified(fake_cloud: dict[str, str]) -> None:
    manifest_path = _create_candidate(fake_cloud)
    fake_cloud["YC_PROMOTE_APPROVED"] = "1"
    promoted = _run(["promote", str(manifest_path)], fake_cloud)
    assert promoted.returncode == 0, promoted.stderr
    fake_cloud["YC_ROLLBACK_APPROVED"] = "1"

    result = _run(["rollback", str(manifest_path)], fake_cloud)

    assert result.returncode == 0, result.stderr
    assert "Rollback verified" in result.stdout
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "rolled_back"
    assert manifest["checks"]["rollback_smoke"]["dry_run_confirmed"] is True


def test_candidate_fails_on_startup_error_in_logs(fake_cloud: dict[str, str]) -> None:
    fake_cloud["FAKE_VERSION_LOGS"] = json.dumps([{"level": "ERROR", "message": "ModuleNotFoundError: missing"}])

    result = _run("candidate", fake_cloud)

    assert result.returncode != 0
    manifest = json.loads(_manifest_path(fake_cloud).read_text(encoding="utf-8"))
    assert manifest["status"] == "candidate_failed"
    assert manifest["checks"]["candidate_smoke"]["startup_error_detected"] is True


def test_candidate_can_add_pinned_liquipedia_secret_and_shadow_flag(fake_cloud: dict[str, str]) -> None:
    fake_cloud.update(
        {
            "YC_LIQUIPEDIA_SECRET_ID": "liquipedia-secret-id",
            "YC_LIQUIPEDIA_SECRET_VERSION_ID": "liquipedia-secret-version-id",
            "YC_LIQUIPEDIA_SECRET_KEY": "LIQUIPEDIA_API_KEY",
            "YC_ENABLE_LIQUIPEDIA_SHADOW": "1",
        }
    )

    _create_candidate(fake_cloud)
    create_call = next(
        call for call in _calls(fake_cloud) if call[:4] == ["serverless", "function", "version", "create"]
    )
    environment_csv = create_call[create_call.index("--environment") + 1]
    environment = dict(item.split("=", 1) for item in next(csv.reader([environment_csv])))
    secret_flags = [create_call[index + 1] for index, value in enumerate(create_call) if value == "--secret"]

    assert environment["ENABLE_LIQUIPEDIA_SHADOW"] == "1"
    assert (
        "id=liquipedia-secret-id,version-id=liquipedia-secret-version-id,"
        "key=LIQUIPEDIA_API_KEY,environment-variable=LIQUIPEDIA_API_KEY"
    ) in secret_flags


def test_candidate_can_add_pinned_telegram_proxy_secret(fake_cloud: dict[str, str]) -> None:
    fake_cloud.update(
        {
            "YC_TELEGRAM_PROXY_SECRET_ID": "proxy-secret-id",
            "YC_TELEGRAM_PROXY_SECRET_VERSION_ID": "proxy-secret-version-id",
        }
    )

    _create_candidate(fake_cloud)
    create_call = next(
        call for call in _calls(fake_cloud) if call[:4] == ["serverless", "function", "version", "create"]
    )
    secret_flags = [create_call[index + 1] for index, value in enumerate(create_call) if value == "--secret"]
    assert (
        "id=proxy-secret-id,version-id=proxy-secret-version-id,"
        "key=TELEGRAM_PROXY_URL,environment-variable=TELEGRAM_PROXY_URL"
    ) in secret_flags


def test_candidate_can_rotate_pinned_telegram_token(fake_cloud: dict[str, str]) -> None:
    fake_cloud.update(
        {
            "YC_TELEGRAM_TOKEN_SECRET_ID": "telegram-secret-id",
            "YC_TELEGRAM_TOKEN_SECRET_VERSION_ID": "telegram-secret-version-id",
        }
    )

    _create_candidate(fake_cloud)
    create_call = next(
        call for call in _calls(fake_cloud) if call[:4] == ["serverless", "function", "version", "create"]
    )
    secret_flags = [create_call[index + 1] for index, value in enumerate(create_call) if value == "--secret"]
    assert (
        "id=telegram-secret-id,version-id=telegram-secret-version-id,"
        "key=TELEGRAM_TOKEN,environment-variable=TELEGRAM_TOKEN"
    ) in secret_flags


def test_enabling_liquipedia_shadow_requires_secret_reference(fake_cloud: dict[str, str]) -> None:
    fake_cloud["YC_ENABLE_LIQUIPEDIA_SHADOW"] = "1"

    result = _run("candidate", fake_cloud)

    assert result.returncode != 0
    assert "requires a pinned LIQUIPEDIA_API_KEY" in result.stderr
    assert not any(call[:4] == ["serverless", "function", "version", "create"] for call in _calls(fake_cloud))


def test_check_rejects_payload_without_dry_run(fake_cloud: dict[str, str]) -> None:
    fake_cloud["YC_DRY_RUN_PAYLOAD"] = json.dumps({"dry_run": False})

    result = _run("check", fake_cloud)

    assert result.returncode != 0
    assert "dry_run=true" in result.stderr
    assert _calls(fake_cloud) == []


def test_check_rejects_missing_lockbox_binding(fake_cloud: dict[str, str]) -> None:
    version = dict(BASE_VERSION)
    version["secrets"] = [
        secret for secret in BASE_VERSION["secrets"] if secret["environment_variable"] != "TELEGRAM_TOKEN"
    ]
    fake_cloud["FAKE_BASE_VERSION"] = json.dumps(version)

    result = _run("check", fake_cloud)

    assert result.returncode != 0
    assert "missing pinned Lockbox binding: TELEGRAM_TOKEN" in result.stderr


def test_deploy_command_is_removed(fake_cloud: dict[str, str]) -> None:
    result = _run("deploy", fake_cloud)

    assert result.returncode != 0
    assert "run candidate" in result.stderr
    assert _calls(fake_cloud) == []


@pytest.mark.parametrize("policy", [
    {"Statement": [{"Effect": "Allow", "Principal": {"AWS": "*"}}]},
    {"Statement": {"Effect": "Allow", "NotPrincipal": {"CanonicalUser": "one-user"}}},
    "not valid JSON",
])
def test_package_policy_cannot_bypass_privacy_check(fake_cloud, policy):
    fake_cloud.update({"YC_FUNCTION_PACKAGE_BUCKET": "package-bucket", "FAKE_BUCKET_POLICY": json.dumps(policy)})
    result = _run("candidate", fake_cloud)
    assert result.returncode != 0
    assert not any(call[:3] == ["storage", "s3api", "put-object"] for call in _calls(fake_cloud))


def test_smoke_rejects_a_candidate_tag_changed_during_invocation(fake_cloud):
    fake_cloud["FAKE_CHANGE_CANDIDATE_DURING_SMOKE"] = "1"
    result = _run("candidate", fake_cloud)
    assert result.returncode != 0
    manifest = json.loads(_manifest_path(fake_cloud).read_text())
    assert manifest["checks"]["candidate_smoke"]["error"] == "tag_mismatch_after_smoke"


def test_lost_tag_response_is_reconciled_for_promote_and_rollback(fake_cloud):
    manifest = _create_candidate(fake_cloud)
    fake_cloud.update({"YC_PROMOTE_APPROVED": "1", "YC_ROLLBACK_APPROVED": "1", "FAKE_TAG_RESPONSE_LOST": "1"})
    result = _run(["promote", str(manifest)], fake_cloud)
    assert result.returncode == 0, result.stderr
    result = _run(["rollback", str(manifest)], fake_cloud)
    assert result.returncode == 0, result.stderr
    assert json.loads(manifest.read_text())["status"] == "rolled_back"


def test_manifest_upload_failure_does_not_leave_unchecked_production(fake_cloud):
    fake_cloud["YC_FUNCTION_PACKAGE_BUCKET"] = "package-bucket"
    manifest = _create_candidate(fake_cloud)
    fake_cloud.update({"YC_PROMOTE_APPROVED": "1", "FAKE_FAIL_MANIFEST_AFTER_PROMOTE": "1"})
    result = _run(["promote", str(manifest)], fake_cloud)
    assert result.returncode != 0
    assert json.loads(manifest.read_text())["status"] == "rolled_back"
    assert json.loads(Path(fake_cloud["FAKE_STATE_PATH"]).read_text())["production"] == "production-version"


def test_rollback_refuses_to_overwrite_a_later_release(fake_cloud):
    manifest = _create_candidate(fake_cloud)
    fake_cloud["YC_PROMOTE_APPROVED"] = "1"
    assert _run(["promote", str(manifest)], fake_cloud).returncode == 0
    path = Path(fake_cloud["FAKE_STATE_PATH"])
    state = json.loads(path.read_text())
    state["production"] = "later-release"
    path.write_text(json.dumps(state))
    fake_cloud["YC_ROLLBACK_APPROVED"] = "1"
    result = _run(["rollback", str(manifest)], fake_cloud)
    assert result.returncode != 0
    assert "Rollback refused" in result.stderr
    assert json.loads(path.read_text())["production"] == "later-release"


def test_smoke_uses_manifest_payload_after_local_environment_changes(fake_cloud):
    fake_cloud["YC_DRY_RUN_PAYLOAD"] = '{"job":"analytics","analytics_operation":"snapshot","dry_run":true}'
    manifest = _create_candidate(fake_cloud)
    fake_cloud.update({"YC_PROMOTE_APPROVED": "1", "YC_DRY_RUN_PAYLOAD": '{"limit":10,"dry_run":true}'})
    assert _run(["promote", str(manifest)], fake_cloud).returncode == 0
    calls = [call for call in _calls(fake_cloud) if call[:3] == ["serverless", "function", "invoke"]]
    assert all(json.loads(call[call.index("--data") + 1])["job"] == "analytics" for call in calls)


def test_timer_envelope_cannot_override_smoke_dry_run(fake_cloud):
    fake_cloud["YC_DRY_RUN_PAYLOAD"] = json.dumps({
        "dry_run": True, "messages": [{"details": {"payload": '{"dry_run":false}'}}],
    })
    result = _run("check", fake_cloud)
    assert result.returncode != 0
    assert _calls(fake_cloud) == []


def test_smoke_requires_boolean_dry_run_confirmation(fake_cloud):
    fake_cloud["FAKE_STRING_DRY_RUN"] = "1"
    result = _run("candidate", fake_cloud)
    assert result.returncode != 0
    assert json.loads(_manifest_path(fake_cloud).read_text())["status"] == "candidate_failed"


def test_existing_candidate_final_cards_override_and_required_binding(fake_cloud):
    version = json.loads(json.dumps(BASE_VERSION))
    version["environment"]["ENABLE_LIQUIPEDIA_FINAL_CARDS"] = "1"
    fake_cloud["FAKE_BASE_VERSION"] = json.dumps(version)
    assert _run("check", fake_cloud).returncode != 0
    version["secrets"].append({
        "environment_variable": "LIQUIPEDIA_API_KEY", "id": "liquipedia-secret",
        "version_id": "pinned", "key": "LIQUIPEDIA_API_KEY",
    })
    fake_cloud["FAKE_BASE_VERSION"] = json.dumps(version)
    assert _run("candidate", fake_cloud).returncode == 0

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


def _triggers(tag: str | None = "production") -> list[dict[str, object]]:
    function_target: dict[str, str] = {"function_id": FUNCTION_ID}
    if tag is not None:
        function_target["function_tag"] = tag
    return [
        {
            "name": "results",
            "rule": {"timer": {"invoke_function_with_retry": function_target}},
        }
    ]


@pytest.fixture
def fake_cloud(tmp_path: Path) -> dict[str, str]:
    yc_path = tmp_path / "yc"
    build_path = tmp_path / "build.sh"
    zip_path = tmp_path / "function.zip"
    log_path = tmp_path / "yc.log"
    promoted_path = tmp_path / "promoted"

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
promoted_path = pathlib.Path(os.environ["FAKE_PROMOTED_PATH"])

if args[:3] == ["serverless", "function", "get"]:
    print(json.dumps({"id": os.environ["YC_FUNCTION_ID"], "folder_id": os.environ["YC_FOLDER_ID"]}))
elif args[:4] == ["serverless", "function", "version", "get-by-tag"]:
    if promoted_path.exists():
        base = dict(base, id="candidate-version")
    print(json.dumps(base))
elif args[:3] == ["serverless", "trigger", "list"]:
    print(os.environ["FAKE_TRIGGERS"])
elif args[:4] == ["serverless", "function", "version", "create"]:
    print(json.dumps({"id": "candidate-version", "status": "ACTIVE"}))
elif args[:3] == ["serverless", "function", "invoke"]:
    print(json.dumps({"statusCode": 200, "body": json.dumps({"dry_run": True, "messages_sent": 0})}))
elif args[:4] == ["serverless", "function", "version", "set-tag"]:
    promoted_path.touch()
    print(json.dumps({"id": "candidate-version", "status": "ACTIVE"}))
else:
    print("unsupported fake yc call: " + repr(args), file=sys.stderr)
    raise SystemExit(2)
""",
        encoding="utf-8",
    )
    yc_path.chmod(0o755)

    build_path.write_text(
        f"#!/usr/bin/env bash\nset -euo pipefail\ntouch {zip_path!s}\nprintf '%s\\n' {zip_path!s}\n",
        encoding="utf-8",
    )
    build_path.chmod(0o755)

    return {
        **os.environ,
        "YC_BIN": str(yc_path),
        "JQ_BIN": "/usr/bin/jq",
        "YC_BUILD_SCRIPT": str(build_path),
        "YC_FUNCTION_ID": FUNCTION_ID,
        "YC_FOLDER_ID": FOLDER_ID,
        "FAKE_BASE_VERSION": json.dumps(BASE_VERSION),
        "FAKE_TRIGGERS": json.dumps(_triggers()),
        "FAKE_YC_LOG": str(log_path),
        "FAKE_PROMOTED_PATH": str(promoted_path),
    }


def _run(command: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(DEPLOY_SCRIPT), command],
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


def test_check_is_read_only(fake_cloud: dict[str, str]) -> None:
    result = _run("check", fake_cloud)

    assert result.returncode == 0, result.stderr
    assert "Preflight passed" in result.stdout
    assert not any(call[:4] == ["serverless", "function", "version", "create"] for call in _calls(fake_cloud))


def test_check_rejects_trigger_without_production_tag(fake_cloud: dict[str, str]) -> None:
    fake_cloud["FAKE_TRIGGERS"] = json.dumps(_triggers(tag=None))

    result = _run("check", fake_cloud)

    assert result.returncode != 0
    assert "not pinned to tag production" in result.stderr
    assert not any(call[:4] == ["serverless", "function", "version", "create"] for call in _calls(fake_cloud))


def test_deploy_preserves_configuration_and_promotes_after_dry_run(fake_cloud: dict[str, str]) -> None:
    fake_cloud["YC_DEPLOY_APPROVED"] = "1"

    result = _run("deploy", fake_cloud)

    assert result.returncode == 0, result.stderr
    assert "Deploy complete" in result.stdout
    calls = _calls(fake_cloud)

    create_index = next(i for i, call in enumerate(calls) if call[:4] == ["serverless", "function", "version", "create"])
    invoke_index = next(i for i, call in enumerate(calls) if call[:3] == ["serverless", "function", "invoke"])
    promote_index = next(i for i, call in enumerate(calls) if call[:4] == ["serverless", "function", "version", "set-tag"])
    assert create_index < invoke_index < promote_index

    create_call = calls[create_index]
    environment_csv = create_call[create_call.index("--environment") + 1]
    environment = dict(item.split("=", 1) for item in next(csv.reader([environment_csv])))
    assert environment == BASE_VERSION["environment"]
    assert create_call[create_call.index("--memory") + 1] == "256MB"
    assert create_call[create_call.index("--service-account-id") + 1] == "service-account-id"
    assert create_call[create_call.index("--min-log-level") + 1] == "info"

    secret_flags = [
        create_call[index + 1]
        for index, value in enumerate(create_call)
        if value == "--secret"
    ]
    assert len(secret_flags) == 4
    assert all("id=lockbox-id" in value for value in secret_flags)
    assert all("version-id=lockbox-version-id" in value for value in secret_flags)


def test_first_liquipedia_deploy_adds_pinned_secret_and_shadow_flag(
    fake_cloud: dict[str, str],
) -> None:
    fake_cloud.update(
        {
            "YC_DEPLOY_APPROVED": "1",
            "YC_LIQUIPEDIA_SECRET_ID": "liquipedia-secret-id",
            "YC_LIQUIPEDIA_SECRET_VERSION_ID": "liquipedia-secret-version-id",
            "YC_LIQUIPEDIA_SECRET_KEY": "LIQUIPEDIA_API_KEY",
            "YC_ENABLE_LIQUIPEDIA_SHADOW": "1",
        }
    )

    result = _run("deploy", fake_cloud)

    assert result.returncode == 0, result.stderr
    create_call = next(
        call
        for call in _calls(fake_cloud)
        if call[:4] == ["serverless", "function", "version", "create"]
    )
    environment_csv = create_call[create_call.index("--environment") + 1]
    environment = dict(item.split("=", 1) for item in next(csv.reader([environment_csv])))
    assert environment["ENABLE_LIQUIPEDIA_SHADOW"] == "1"

    secret_flags = [
        create_call[index + 1]
        for index, value in enumerate(create_call)
        if value == "--secret"
    ]
    assert len(secret_flags) == 5
    assert (
        "id=liquipedia-secret-id,version-id=liquipedia-secret-version-id,"
        "key=LIQUIPEDIA_API_KEY,environment-variable=LIQUIPEDIA_API_KEY"
    ) in secret_flags


def test_enabling_liquipedia_shadow_requires_secret_reference(
    fake_cloud: dict[str, str],
) -> None:
    fake_cloud["YC_DEPLOY_APPROVED"] = "1"
    fake_cloud["YC_ENABLE_LIQUIPEDIA_SHADOW"] = "1"

    result = _run("deploy", fake_cloud)

    assert result.returncode != 0
    assert "requires a pinned LIQUIPEDIA_API_KEY" in result.stderr
    assert not any(
        call[:4] == ["serverless", "function", "version", "create"]
        for call in _calls(fake_cloud)
    )


def test_deploy_requires_explicit_approval(fake_cloud: dict[str, str]) -> None:
    result = _run("deploy", fake_cloud)

    assert result.returncode != 0
    assert "YC_DEPLOY_APPROVED=1" in result.stderr
    assert _calls(fake_cloud) == []


def test_check_rejects_payload_without_dry_run(fake_cloud: dict[str, str]) -> None:
    fake_cloud["YC_DRY_RUN_PAYLOAD"] = json.dumps({"dry_run": False})

    result = _run("check", fake_cloud)

    assert result.returncode != 0
    assert "dry_run=true" in result.stderr
    assert _calls(fake_cloud) == []


def test_check_rejects_missing_lockbox_binding(fake_cloud: dict[str, str]) -> None:
    version = dict(BASE_VERSION)
    version["secrets"] = [
        secret
        for secret in BASE_VERSION["secrets"]
        if secret["environment_variable"] != "TELEGRAM_TOKEN"
    ]
    fake_cloud["FAKE_BASE_VERSION"] = json.dumps(version)

    result = _run("check", fake_cloud)

    assert result.returncode != 0
    assert "missing pinned Lockbox binding: TELEGRAM_TOKEN" in result.stderr
    assert not any(call[:4] == ["serverless", "function", "version", "create"] for call in _calls(fake_cloud))


def test_check_rejects_enabled_liquipedia_without_lockbox_binding(
    fake_cloud: dict[str, str],
) -> None:
    version = json.loads(json.dumps(BASE_VERSION))
    version["environment"]["ENABLE_LIQUIPEDIA_SHADOW"] = "1"
    fake_cloud["FAKE_BASE_VERSION"] = json.dumps(version)

    result = _run("check", fake_cloud)

    assert result.returncode != 0
    assert "missing pinned Lockbox binding: LIQUIPEDIA_API_KEY" in result.stderr

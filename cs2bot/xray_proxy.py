"""Ephemeral local Xray HTTP proxy for short-lived serverless invocations."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class XrayProxyError(RuntimeError):
    """Raised when a local Xray client cannot be started safely."""


def _binary_path() -> Path:
    override = os.getenv("XRAY_BINARY_PATH", "").strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[1] / "xray" / "xray"


def _config_with_http_inbound(raw: str, port: int) -> str:
    try:
        config = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise XrayProxyError("XRAY_CONFIG_JSON is not valid JSON") from exc
    if not isinstance(config, dict) or not isinstance(config.get("outbounds"), list):
        raise XrayProxyError("XRAY_CONFIG_JSON must contain an outbounds list")
    inbounds = config.setdefault("inbounds", [])
    if not isinstance(inbounds, list):
        raise XrayProxyError("XRAY_CONFIG_JSON inbounds must be a list")
    inbounds.append(
        {
            "tag": "cs2results-local-http",
            "listen": "127.0.0.1",
            "port": port,
            "protocol": "http",
            "settings": {"allowTransparent": False},
        }
    )
    return json.dumps(config, separators=(",", ":"))


def _wait_until_listening(process: subprocess.Popen[bytes], port: int) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise XrayProxyError("Xray client exited during startup")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.1)
    raise XrayProxyError("Xray client did not open its local HTTP proxy")


def _available_loopback_port() -> int:
    """Allocate a currently unused loopback port for one Xray invocation."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


@contextmanager
def xray_http_proxy() -> Iterator[dict[str, str] | None]:
    """Start Xray only when its Lockbox-provided client config is present."""
    raw_config = os.getenv("XRAY_CONFIG_JSON", "").strip()
    if not raw_config:
        yield None
        return
    binary = _binary_path()
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise XrayProxyError("Xray binary is unavailable")

    port = _available_loopback_port()
    config_file = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    try:
        config_file.write(_config_with_http_inbound(raw_config, port))
        config_file.close()
        process = subprocess.Popen(
            [str(binary), "run", "-c", config_file.name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            _wait_until_listening(process, port)
            proxy = f"http://127.0.0.1:{port}"
            yield {"http": proxy, "https": proxy}
        finally:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
    finally:
        Path(config_file.name).unlink(missing_ok=True)

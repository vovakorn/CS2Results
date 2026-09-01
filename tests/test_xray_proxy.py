import json

import pytest

import socket

from cs2bot.xray_proxy import XrayProxyError, _available_loopback_port, _config_with_http_inbound


def test_xray_config_adds_loopback_http_inbound():
    config = json.loads(_config_with_http_inbound('{"outbounds": []}', 18080))

    assert config["inbounds"] == [
        {
            "tag": "cs2results-local-http",
            "listen": "127.0.0.1",
            "port": 18080,
            "protocol": "http",
            "settings": {"allowTransparent": False},
        }
    ]


@pytest.mark.parametrize("raw", ["not json", "[]", "{}"])
def test_xray_config_rejects_invalid_client_config(raw):
    with pytest.raises(XrayProxyError):
        _config_with_http_inbound(raw, 18080)


def test_available_loopback_port_is_bindable():
    port = _available_loopback_port()

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", port))

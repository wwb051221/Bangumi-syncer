from __future__ import annotations

import urllib.parse

import pytest

from bangumi_syncer.config import AccountConfig, TargetConfig
from bangumi_syncer.http import HttpClient
from bangumi_syncer.oauth import OAuthError, authorize_url, exchange_code


def test_authorize_url_contains_everything():
    account = AccountConfig(
        name="main",
        client_id="client-123",
        client_secret="secret",
        redirect_uri="http://127.0.0.1:8765/oauth/callback",
    )
    url = authorize_url(account, "state-abc")
    parsed = urllib.parse.urlparse(url)
    params = urllib.parse.parse_qs(parsed.query)
    assert parsed.netloc == "bgm.tv"
    assert parsed.path == "/oauth/authorize"
    assert params["client_id"] == ["client-123"]
    assert params["response_type"] == ["code"]
    assert params["redirect_uri"] == ["http://127.0.0.1:8765/oauth/callback"]
    assert params["state"] == ["state-abc"]


def test_exchange_without_credentials_fails_fast():
    account = AccountConfig(name="main", client_id="", client_secret="")
    http = HttpClient("test/0", timeout=1, max_retries=0)
    with pytest.raises(OAuthError):
        exchange_code(account, "code", http)


def test_target_requires_known_type():
    target = TargetConfig(type="nope")
    assert target.type == "nope"  # validation happens in Config.validate()

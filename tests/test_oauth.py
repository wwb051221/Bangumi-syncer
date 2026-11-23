from __future__ import annotations

import urllib.parse

import pytest

from bangumi_syncer.config import AccountConfig, TargetConfig
from bangumi_syncer.http import HttpClient
from bangumi_syncer.oauth import (
    OAuthError,
    apply_stored_credentials,
    authorize_url,
    exchange_code,
    extract_code,
    save_client,
    stored_client,
)
from bangumi_syncer.state import StateStore


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


def test_extract_code_accepts_url_or_bare_code():
    assert extract_code("abc123") == "abc123"
    assert extract_code("http://127.0.0.1:8765/oauth/callback?code=xyz&state=s") == "xyz"
    assert extract_code("https://example.com/cb?state=s&code=hello") == "hello"
    assert extract_code("   ") == ""


def test_stored_credentials_are_applied(tmp_path, fnos_db):
    from conftest import make_config

    cfg = make_config(fnos_db, str(tmp_path / "state.db"), dry_run=True)
    store = StateStore(str(tmp_path / "state.db"))
    try:
        save_client(store, "main", client_id="123", client_secret="secret",
                    redirect_uri="http://127.0.0.1:9999/cb")
        assert stored_client(store, "main")["client_id"] == "123"
        apply_stored_credentials(cfg, store)
        account = cfg.account("main")
        assert account.client_id == "123"
        assert account.client_secret == "secret"
        assert account.redirect_uri == "http://127.0.0.1:9999/cb"

        # an empty secret keeps the existing one
        save_client(store, "main", client_id="456", client_secret=None)
        saved = stored_client(store, "main")
        assert saved["client_id"] == "456"
        assert saved["client_secret"] == "secret"
    finally:
        store.close()

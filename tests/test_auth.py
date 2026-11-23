from __future__ import annotations

import time

import pytest

from bangumi_syncer.auth import COOKIE_NAME, AuthError, AuthManager
from bangumi_syncer.state import StateStore


@pytest.fixture()
def auth(tmp_path):
    store = StateStore(str(tmp_path / "state.db"))
    manager = AuthManager(store)
    try:
        yield manager, store
    finally:
        store.close()


def cookie_value(header: str) -> str:
    return header.split(";", 1)[0]


def test_first_run_creates_an_admin(auth):
    manager, _store = auth
    assert manager.setup_required() is True
    user = manager.setup("owner", "secret123")
    assert user["role"] == "admin"
    assert manager.setup_required() is False
    assert manager.count() == 1
    assert manager.verify("owner", "secret123") is True
    assert manager.verify("owner", "wrong") is False
    with pytest.raises(AuthError):
        manager.setup("second", "secret123")


def test_validation_rules(auth):
    manager, _store = auth
    with pytest.raises(AuthError):
        manager.setup("ab", "secret123")
    with pytest.raises(AuthError):
        manager.setup("owner", "123")
    with pytest.raises(AuthError):
        manager.setup("bad name", "secret123")
    manager.setup("owner", "secret123")
    with pytest.raises(AuthError):
        manager.create_user("owner", "secret123")
    with pytest.raises(AuthError):
        manager.create_user("kid", "123")


def test_session_cookie_roundtrip(auth):
    manager, _store = auth
    manager.setup("owner", "secret123")
    header = manager.issue_cookie("owner")
    assert header.startswith(COOKIE_NAME + "=")
    assert manager.user_from_cookie(cookie_value(header)) == {"username": "owner", "role": "admin"}

    tampered = header.replace("owner", "other", 1)
    assert manager.user_from_cookie(cookie_value(tampered)) is None
    assert manager.user_from_cookie(cookie_value(header) + "x") is None
    assert manager.user_from_cookie("") is None
    assert manager.user_from_cookie(None) is None


def test_cookie_for_deleted_or_expired_user(auth):
    manager, _store = auth
    manager.setup("owner", "secret123")
    manager.create_user("kid", "secret123", "member")
    header = cookie_value(manager.issue_cookie("kid"))
    assert manager.user_from_cookie(header)["username"] == "kid"
    manager.delete_user("kid", actor="owner")
    assert manager.user_from_cookie(header) is None

    expired = "owner|%d|%s" % (int(time.time()) - 5, manager._sign("owner|%d" % (int(time.time()) - 5)))
    import urllib.parse

    assert manager.user_from_cookie("bs_session=" + urllib.parse.quote(expired)) is None


def test_password_change_and_reset(auth):
    manager, _store = auth
    manager.setup("owner", "secret123")
    with pytest.raises(AuthError):
        manager.change_password("owner", "wrong", "newsecret")
    manager.change_password("owner", "secret123", "newsecret")
    assert manager.verify("owner", "secret123") is False
    assert manager.verify("owner", "newsecret") is True

    manager.create_user("kid", "kidpass1", "member")
    manager.reset_password("kid", "kidpass2")
    assert manager.verify("kid", "kidpass2") is True


def test_admin_guards(auth):
    manager, _store = auth
    manager.setup("owner", "secret123")
    manager.create_user("kid", "kidpass1", "member")

    with pytest.raises(AuthError):
        manager.delete_user("owner", actor="owner")
    with pytest.raises(AuthError):
        manager.set_role("owner", "member")  # last admin
    with pytest.raises(AuthError):
        manager.delete_user("missing", actor="owner")

    manager.set_role("kid", "admin")
    assert manager.get_user("kid")["role"] == "admin"
    manager.delete_user("kid", actor="owner")
    assert manager.get_user("kid") is None


def test_users_persist_across_manager_instances(tmp_path):
    db = str(tmp_path / "state.db")
    store = StateStore(db)
    manager = AuthManager(store)
    manager.setup("owner", "secret123")
    manager.create_user("kid", "kidpass1", "member")
    store.close()

    store = StateStore(db)
    try:
        manager = AuthManager(store)
        assert manager.count() == 2
        assert manager.verify("kid", "kidpass1") is True
        assert manager.get_user("owner")["role"] == "admin"
        # the signing secret is persisted too, so old cookies keep working
        header = cookie_value(manager.issue_cookie("owner"))
        assert manager.user_from_cookie(header)["username"] == "owner"
    finally:
        store.close()

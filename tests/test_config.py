from __future__ import annotations

import pytest

from fnos2bgm import config
from fnos2bgm.config import ConfigError

BASE = """
[app]
dry_run = false
state_db = "/data/state/state.db"
log_level = "WARNING"

[source]
mode = "snapshot"
snapshot_path = "/data/origin.db"
include_paths = ["/vol1/1000/\u5f71\u7247/\u52a8\u6f2b/"]
skip_season_zero = true

[sync]
interval_seconds = 120
watch_ratio = 0.85

[target]
user_agent = "test"

[[target.accounts]]
name = "main"
client_id = "${BGM_CLIENT_ID}"
client_secret = "${BGM_SECRET:-fallback}"

[[users]]
fnos_user = "alice"
account = "main"

[server]
enabled = false
listen = "127.0.0.1:9000"
"""


def test_defaults_and_normalisation(monkeypatch):
    monkeypatch.setenv("BGM_CLIENT_ID", "client-abc")
    monkeypatch.delenv("BGM_SECRET", raising=False)
    cfg = config.loads(BASE)
    assert cfg.app.dry_run is False
    assert cfg.source.include_paths == ["/vol1/1000/\u5f71\u7247/\u52a8\u6f2b"]
    assert cfg.sync.watch_ratio == 0.85
    assert cfg.target.accounts[0].client_id == "client-abc"
    assert cfg.target.accounts[0].client_secret == "fallback"
    assert cfg.server.host_port() == ("127.0.0.1", 9000)
    assert cfg.users[0].enabled is True


def test_missing_env_becomes_empty(monkeypatch):
    monkeypatch.delenv("BGM_CLIENT_ID", raising=False)
    monkeypatch.delenv("BGM_SECRET", raising=False)
    cfg = config.loads(BASE)
    assert cfg.target.accounts[0].client_id == ""
    assert cfg.target.accounts[0].client_secret == "fallback"


def test_bom_is_tolerated(tmp_path):
    path = tmp_path / "config.toml"
    path.write_bytes(b"\xef\xbb\xbf" + BASE.encode("utf-8"))
    cfg = config.load(str(path))
    assert cfg.source.mode == "snapshot"


def test_unknown_user_account_is_rejected():
    broken = BASE.replace('account = "main"', 'account = "nope"')
    with pytest.raises(ConfigError):
        config.loads(broken)


def test_invalid_mode_is_rejected():
    broken = BASE.replace('mode = "snapshot"', 'mode = "telepathy"')
    with pytest.raises(ConfigError):
        config.loads(broken)


def test_no_accounts_is_rejected():
    broken = BASE.replace('[[target.accounts]]', '[[target.other]]')
    with pytest.raises(ConfigError):
        config.loads(broken)


def test_bad_ratio_is_rejected():
    broken = BASE.replace("watch_ratio = 0.85", "watch_ratio = 8.5")
    with pytest.raises(ConfigError):
        config.loads(broken)


def test_interval_floor():
    broken = BASE.replace("interval_seconds = 120", "interval_seconds = 5")
    with pytest.raises(ConfigError):
        config.loads(broken)


def test_duplicate_account_names():
    duplicated = BASE + '\n[[target.accounts]]\nname = "main"\n'
    with pytest.raises(ConfigError):
        config.loads(duplicated)


def test_redacted_hides_secret(monkeypatch):
    monkeypatch.setenv("BGM_CLIENT_ID", "id")
    monkeypatch.setenv("BGM_SECRET", "very-secret")
    cfg = config.loads(BASE)
    assert cfg.redacted()["target"]["accounts"][0]["client_secret"] == "***"
    assert "very-secret" not in str(cfg.redacted())


def test_unknown_keys_are_ignored(caplog):
    noisy = BASE.replace("[app]", "[app]\nnot_a_real_key = 42")
    cfg = config.loads(noisy)  # must not raise
    assert cfg.app.dry_run is False


def test_ipv6_listen():
    cfg = config.loads(BASE.replace('listen = "127.0.0.1:9000"', 'listen = "[::]:8099"'))
    assert cfg.server.host_port() == ("::", 8099)


def test_missing_file_raises(tmp_path):
    with pytest.raises(ConfigError):
        config.load(str(tmp_path / "nope.toml"))

from __future__ import annotations

import pytest
from conftest import make_config

from bangumi_syncer.settings import SettingsError, SettingsManager
from bangumi_syncer.state import StateStore


@pytest.fixture()
def manager(tmp_path, fnos_db):
    cfg = make_config(fnos_db, str(tmp_path / "state.db"), dry_run=True)
    store = StateStore(cfg.app.state_db)
    instance = SettingsManager(cfg, store)
    try:
        yield instance, cfg, store
    finally:
        store.close()


def test_values_reflect_config(manager):
    instance, cfg, _store = manager
    values = instance.values()
    assert values["dry_run"] is True
    assert values["interval_seconds"] == 300
    assert values["watch_ratio"] == 0.9
    assert [u["fnos_user"] for u in values["users"]] == ["alice", "bob"]
    assert values["include_paths"] == ["/vol1/1000/\u5f71\u7247/\u52a8\u6f2b"]


def test_update_applies_and_persists(manager):
    instance, cfg, store = manager
    values = instance.update({
        "dry_run": False,
        "interval_seconds": 120,
        "watch_ratio": 0.75,
        "include_paths": ["/vol1/1000/影片/动漫/本季新番/"],
        "match": {"min_score": 0.9},
        "users": [{"fnos_user": "alice", "account": "main", "enabled": True, "watch_ratio": 0.95}],
    })
    assert values["dry_run"] is False
    assert cfg.app.dry_run is False
    assert cfg.sync.interval_seconds == 120
    assert cfg.sync.watch_ratio == 0.75
    assert cfg.source.include_paths == ["/vol1/1000/影片/动漫/本季新番"]
    assert cfg.match.min_score == 0.9
    assert [u.fnos_user for u in cfg.users] == ["alice"]
    assert cfg.users[0].watch_ratio == 0.95

    # a new manager (simulating a process restart) sees the persisted overlay
    reloaded = SettingsManager(cfg, store)
    assert reloaded.values()["interval_seconds"] == 120
    assert reloaded.values()["users"][0]["watch_ratio"] == 0.95


def test_reset_restores_toml_values(manager):
    instance, cfg, _store = manager
    instance.update({"dry_run": False, "interval_seconds": 600})
    instance.reset()
    values = instance.values()
    assert values["dry_run"] is True
    assert values["interval_seconds"] == 300
    assert cfg.app.dry_run is True


@pytest.mark.parametrize("payload,needle", [
    ({"interval_seconds": 5}, "interval_seconds"),
    ({"watch_ratio": 5}, "watch_ratio"),
    ({"min_episodes": 0}, "min_episodes"),
    ({"match": {"search_limit": 99}}, "search_limit"),
    ({"users": []}, "users"),
    ({"users": [{"fnos_user": "", "account": "main"}]}, "fnos_user"),
    ({"users": [{"fnos_user": "alice", "account": "nope"}]}, "unknown account"),
])
def test_validation_errors(manager, payload, needle):
    instance, _cfg, _store = manager
    with pytest.raises(SettingsError) as excinfo:
        instance.update(payload)
    assert needle in str(excinfo.value)


def test_user_duplicates_are_collapsed(manager):
    instance, _cfg, _store = manager
    values = instance.update({"users": [
        {"fnos_user": "alice", "account": "main", "enabled": True},
        {"fnos_user": "alice", "account": "main", "enabled": False},
    ]})
    assert len(values["users"]) == 1
    assert values["users"][0]["enabled"] is True


def test_disabled_users_are_not_synced(manager):
    instance, cfg, _store = manager
    instance.update({"users": [
        {"fnos_user": "alice", "account": "main", "enabled": False},
        {"fnos_user": "bob", "account": "main", "enabled": True},
    ]})
    assert [u.fnos_user for u in cfg.enabled_users()] == ["bob"]


def test_source_users_come_from_the_media_server(manager):
    instance, _cfg, _store = manager
    source = instance.source_users()
    names = {user["name"] for user in source["users"]}
    assert names == {"alice", "bob"}
    alice = next(user for user in source["users"] if user["name"] == "alice")
    assert alice["plays"] == 3
    # ep1 (watched=1), ep2 (90.3% by ratio) and episode 30 of the merged show
    assert alice["finished"] == 3
    assert source["seasons"] == 2
    assert source["episodes"] == 76


def test_source_users_include_the_folder_tree(manager):
    instance, _cfg, _store = manager
    folders = instance.source_users()["folders"]
    assert folders["series"] == 3  # two anime seasons + one drama
    root = folders["roots"][0]
    assert root["path"] == "/vol1/1000/影片"
    assert {child["path"] for child in root["children"]} == {
        "/vol1/1000/影片/动漫",
        "/vol1/1000/影片/电视剧/大明王朝1566",
    }
    anime = next(child for child in root["children"] if child["name"] == "动漫")
    assert anime["series"] == 2
    assert anime["episodes"] == 76

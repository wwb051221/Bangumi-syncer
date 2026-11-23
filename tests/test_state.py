from __future__ import annotations

import time

import pytest

from fnos2bgm.state import StateStore, Tokens


@pytest.fixture()
def store(tmp_path) -> StateStore:
    store = StateStore(str(tmp_path / "state.db"))
    yield store
    store.close()


def test_schema_version(store):
    assert store.meta("schema_version") == "1"


def test_narrowest_mapping_range_wins(store):
    store.save_mapping("main", "k", 100, "base", strategy="manual", ep_from=1, ep_to=9999)
    store.save_mapping("main", "k", 200, "season2", strategy="manual", ep_from=25, ep_to=48)
    assert store.mapping_for("main", "k", 1)["subject_id"] == 100
    assert store.mapping_for("main", "k", 24)["subject_id"] == 100
    assert store.mapping_for("main", "k", 25)["subject_id"] == 200
    assert store.mapping_for("main", "k", 48)["subject_id"] == 200
    assert store.mapping_for("main", "k", 49)["subject_id"] == 100


def test_mapping_upsert(store):
    store.save_mapping("main", "k", 100, "a", strategy="auto", confidence=0.5)
    store.save_mapping("main", "k", 100, "b", strategy="manual", confidence=1.0, confirmed=True)
    rows = store.mappings("main")
    assert len(rows) == 1
    assert rows[0]["subject_name"] == "b"
    assert rows[0]["confirmed"] == 1


def test_mapping_delete(store):
    store.save_mapping("main", "k", 100, "a", strategy="manual", ep_from=1, ep_to=24)
    store.save_mapping("main", "k", 100, "b", strategy="manual", ep_from=25, ep_to=48)
    assert store.delete_mapping("main", "k", 25) == 1
    assert len(store.mapping_groups("main", "k")) == 1
    assert store.delete_mapping("main", "k") == 1
    assert store.mapping_groups("main", "k") == []


def test_push_lifecycle(store):
    assert store.push("main", "k", 1) is None
    store.record_push("main", "k", 1, "failed", subject_id=10, episode_id=20, error="boom")
    row = store.push("main", "k", 1)
    assert row["state"] == "failed"
    assert store.pushes("main", state="failed")[0]["error"] == "boom"

    store.record_push("main", "k", 1, "sent", subject_id=10, episode_id=20, watched=True)
    row = store.push("main", "k", 1)
    assert row["state"] == "sent"
    assert row["attempts"] == 1  # one extra attempt after the initial insert
    assert store.stats()["pushed"] == 1


def test_delete_pushes_range(store):
    for episode in range(1, 6):
        store.record_push("main", "k", episode, "sent")
    assert store.delete_pushes("main", "k", 2, 4) == 3
    assert {r["episode"] for r in store.pushes("main")} == {1, 5}
    assert store.delete_pushes("main", "k") == 2


def test_tokens_roundtrip(store):
    assert store.tokens("main") is None
    expires = int(time.time()) + 3600
    store.save_tokens("main", Tokens("access", "refresh", expires, "alice"))
    tokens = store.tokens("main")
    assert tokens is not None
    assert tokens.access_token == "access"
    assert tokens.remote_user == "alice"
    assert not tokens.needs_refresh()
    store.save_tokens("main", Tokens("access2", "refresh2", int(time.time()) - 10))
    assert store.tokens("main").needs_refresh()
    store.clear_tokens("main")
    assert store.tokens("main") is None
    assert store.accounts()[0]["has_token"] == 0


def test_candidates(store):
    store.save_candidates("main", "k", {"title": "x", "candidates": [{"subject_id": 1}]})
    rows = store.candidates("main")
    assert rows[0]["payload"]["title"] == "x"
    store.delete_candidates("main", "k")
    assert store.candidates("main") == []


def test_runs(store):
    run_id = store.start_run()
    store.finish_run(run_id, True, users=1, pushed=2)
    last = store.last_run()
    assert last["ok"] == 1
    assert last["pushed"] == 2
    assert last["finished_at"] >= last["started_at"]

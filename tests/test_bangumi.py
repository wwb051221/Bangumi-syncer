"""Bangumi tracker tests with a scripted HTTP double (no network)."""

from __future__ import annotations

import json
import time

import pytest

from bangumi_syncer.config import AccountConfig, TargetConfig
from bangumi_syncer.http import ApiError, Response
from bangumi_syncer.state import StateStore, Tokens
from bangumi_syncer.trackers import EP_COLLECTED, AuthError
from bangumi_syncer.trackers.bangumi import BangumiTracker


class FakeHttp:
    """Replays a list of (status, payload) tuples or raises the given exception."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def request(self, method: str, url: str, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        status, payload = item
        body = b"" if payload is None else json.dumps(payload).encode()
        return Response(status, {}, body, url)


def build(tmp_path, responses, *, with_token: bool = True):
    store = StateStore(str(tmp_path / "state.db"))
    if with_token:
        store.save_tokens("main", Tokens("access", "refresh", int(time.time()) + 3600))
    account = AccountConfig(name="main", client_id="id", client_secret="secret")
    target = TargetConfig(user_agent="bangumi-syncer-test/0", accounts=[account])
    http = FakeHttp(responses)
    return BangumiTracker(account, target, store, http=http), http, store


def test_search_passes_types_and_parses_hits(tmp_path):
    tracker, http, store = build(tmp_path, [(
        200,
        {"total": 1, "data": [
            {"id": 622288, "name": "FX戦士くるみちゃん", "name_cn": "FX战士久留美",
             "date": "2026-10-01", "eps": 12, "type": 2, "rating": {"score": 7.1}},
        ]},
    )])
    hits = tracker.search("FX战士久留美", 5, [2, 6])
    assert http.calls[0]["json_body"] == {"keyword": "FX战士久留美", "filter": {"type": [2, 6]}}
    assert http.calls[0]["method"] == "POST"
    assert [h.subject_id for h in hits] == [622288]
    assert hits[0].year == 2026 and hits[0].episode_count == 12
    store.close()


def test_search_defaults_to_anime_type(tmp_path):
    tracker, http, store = build(tmp_path, [(200, {"total": 0, "data": []})])
    tracker.search("x")
    assert http.calls[0]["json_body"]["filter"]["type"] == [2]
    store.close()


def test_episodes_pagination(tmp_path):
    first = [{"id": 1000 + i, "sort": float(i + 1), "name": "e%d" % (i + 1)} for i in range(100)]
    second = [{"id": 1100, "sort": 101.0}, {"id": 1101, "sort": 101.5}]
    tracker, http, store = build(tmp_path, [
        (200, {"total": 102, "data": first}),
        (200, {"total": 102, "data": second}),
    ])
    episodes = tracker.episodes(420628)
    assert len(episodes) == 102
    assert episodes[0].number == 1.0
    assert episodes[-1].number == 101.5
    assert "offset=100" in http.calls[1]["url"]
    store.close()


def test_mark_episode(tmp_path):
    tracker, http, store = build(tmp_path, [(204, None)])
    tracker.mark_episode(420628, 1244591)
    call = http.calls[0]
    assert call["method"] == "PUT"
    assert call["url"].endswith("/v0/users/-/collections/420628/episodes/1244591")
    assert call["json_body"] == {"type": EP_COLLECTED}
    assert call["headers"]["Authorization"] == "Bearer access"
    store.close()


def test_collection_missing_returns_none(tmp_path):
    tracker, _http, store = build(tmp_path, [ApiError(404, "u", "not found")])
    assert tracker.collection(420628) is None
    store.close()


def test_set_collection_creates_then_patches(tmp_path):
    tracker, http, store = build(tmp_path, [
        ApiError(404, "u", "not found"),
        (202, None),
    ])
    tracker.set_collection(420628, 3)
    assert http.calls[1]["method"] == "POST"
    assert http.calls[1]["json_body"] == {"type": 3, "comment": ""}
    store.close()


def test_set_collection_patches_existing(tmp_path):
    tracker, http, store = build(tmp_path, [
        (200, {"type": 3}),
        (204, None),
    ])
    tracker.set_collection(420628, 2)
    assert http.calls[1]["method"] == "PATCH"
    assert http.calls[1]["json_body"] == {"type": 2}
    store.close()


def test_episode_marks_accepts_both_shapes(tmp_path):
    tracker, _http, store = build(tmp_path, [(
        200,
        {"total": 2, "data": [{"episode": {"id": 10}, "type": 2}, {"id": 11, "type": 2}]},
    )])
    assert tracker.episode_marks(420628) == {10: 2, 11: 2}
    store.close()


def test_missing_token_raises_auth_error(tmp_path):
    tracker, _http, store = build(tmp_path, [], with_token=False)
    with pytest.raises(AuthError):
        tracker.mark_episode(420628, 1)
    store.close()

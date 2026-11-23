from __future__ import annotations

import json
import logging
import socket
import time
import urllib.error
import urllib.request

import pytest
from conftest import make_config

from bangumi_syncer.config import ServerConfig
from bangumi_syncer.server import StatusServer, StatusState, build_status
from bangumi_syncer.settings import SettingsManager
from bangumi_syncer.state import StateStore
from bangumi_syncer.ui import UiManager

HEADER = {"X-Requested-With": "bangumi-syncer"}


def free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class Client:
    def __init__(self, base: str) -> None:
        self.base = base

    def get(self, path: str):
        with urllib.request.urlopen(self.base + path, timeout=10) as response:
            return response.status, response.read()

    def get_json(self, path: str):
        status, body = self.get(path)
        return status, json.loads(body or b"{}")

    def send(self, method: str, path: str, body=None, header: bool = True):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if data is not None else {}
        if header:
            headers.update(HEADER)
        request = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def send_raw(self, method: str, path: str, data: bytes, content_type: str):
        headers = {"Content-Type": content_type, **HEADER}
        request = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")


@pytest.fixture()
def server(tmp_path, fnos_db):
    cfg = make_config(fnos_db, str(tmp_path / "state.db"), dry_run=True)
    cfg.server = ServerConfig(enabled=True, listen="127.0.0.1:%d" % free_port())
    store = StateStore(cfg.app.state_db)
    store.save_mapping("main", "fnos:abc:s1", subject_id=1940, subject_name="魔卡少女樱",
                       strategy="auto", confidence=1.0)
    store.save_candidates("main", "fnos:def:s1", {
        "title": "未匹配番组",
        "season": 1,
        "year": 2016,
        "episode_count": 21,
        "reason": "best candidate 0.394 < min_score 0.860",
        "candidates": [{"subject_id": 148295, "name": "茶啊二中", "name_cn": "茶啊二中",
                        "year": 2014, "eps": 10, "score": 0.394}],
    })
    state = StatusState(started_at=int(time.time()) - 5, dry_run=True)
    settings = SettingsManager(cfg, store)
    ui = UiManager(store, str(tmp_path))
    instance = StatusServer(cfg, store, state, settings=settings, ui=ui)
    instance.start()
    client = Client(instance.address)
    try:
        yield client, instance, store
    finally:
        instance.stop()
        store.close()


def test_index_serves_the_spa(server):
    client, _instance, _store = server
    status, body = client.get("")
    text = body.decode("utf-8")
    assert status == 200
    assert '<div id="app"' not in text  # it is <main id="app">
    assert 'id="app"' in text
    assert "/static/app.js" in text
    assert "/static/style.css" in text
    assert "Bangumi-syncer" in text


def test_static_assets(server):
    client, _instance, _store = server
    status, body = client.get("static/app.js")
    script = body.decode("utf-8")
    assert status == 200
    assert "X-Requested-With" in script
    assert "api/map" in script

    status, body = client.get("static/style.css")
    assert status == 200
    assert b"--accent" in body


def test_healthz(server):
    client, _instance, _store = server
    status, payload = client.get_json("healthz")
    assert status == 200
    assert payload["ok"] is True


def test_status_api_exposes_ui_state(server):
    client, instance, _store = server
    status, payload = client.get_json("api/status")
    assert status == 200
    assert payload["dry_run"] is True
    assert payload["stats"]["mappings"] == 1
    assert len(payload["review"]) == 1
    assert payload["review"][0]["title"] == "未匹配番组"
    assert payload["mappings"][0]["subject_url"].endswith("/1940")
    # secrets are redacted inside the embedded config
    assert payload["config"]["target"]["accounts"][0]["client_secret"] == "***"
    assert payload["run_requested"] is False
    assert payload["users"][0]["fnos_user"] == "alice"


def test_run_endpoint_requires_header(server):
    client, instance, _store = server
    status, payload = client.send("POST", "api/run", header=False)
    assert status == 403
    assert "X-Requested-With" in payload["error"]
    assert not instance.state.run_requested.is_set()

    status, payload = client.send("POST", "api/run")
    assert status == 202 and payload["queued"] is True
    assert instance.state.run_requested.is_set()

    assert client.send("POST", "api/run")[0] == 409  # already queued


def test_map_create_and_delete(server):
    client, _instance, store = server
    store.record_push("main", "fnos:def:s1", 1, "sent")
    status, payload = client.send("POST", "api/map", {
        "account": "main", "series_key": "fnos:def:s1", "subject_id": 148295,
        "ep_from": 1, "ep_to": 21, "name": "茶啊二中",
    })
    assert status == 200 and payload["ok"] is True
    assert payload["cleared_pushes"] == 1
    row = store.mapping_for("main", "fnos:def:s1", 5)
    assert row is not None and row["subject_id"] == 148295 and row["confirmed"] == 1
    assert store.candidates("main") == []

    status, payload = client.send(
        "DELETE", "api/map?account=main&series_key=fnos%3Adef%3As1&ep_from=1")
    assert status == 200 and payload["removed"] == 1
    assert store.mapping_groups("main", "fnos:def:s1") == []


def test_map_validation(server):
    client, _instance, _store = server
    bad = [
        {"account": "nope", "series_key": "k", "subject_id": 1},
        {"account": "main", "series_key": "", "subject_id": 1},
        {"account": "main", "series_key": "k", "subject_id": 0},
        {"account": "main", "series_key": "k", "subject_id": 1, "ep_from": 5, "ep_to": 2},
    ]
    for body in bad:
        status, payload = client.send("POST", "api/map", body)
        assert status == 400, (body, payload)
        assert "error" in payload


def test_candidate_delete(server):
    client, _instance, store = server
    status, _payload = client.send(
        "DELETE", "api/candidates?account=main&series_key=fnos%3Adef%3As1")
    assert status == 200
    assert store.candidates("main") == []


def test_search_requires_query(server):
    client, _instance, _store = server
    status, payload = client.send("GET", "api/search")
    # GET does not need the write header; it fails on the missing query instead
    assert status == 400
    assert "q is required" in payload["error"]


def test_unknown_route(server):
    client, _instance, _store = server
    status, payload = client.send("GET", "api/nope")
    assert status == 404


def test_log_ring_is_exposed(server):
    client, instance, _store = server
    logging.getLogger("test.ring").warning("ring-buffer-hello")
    status, payload = client.get_json("api/logs?limit=50")
    assert status == 200
    assert any("ring-buffer-hello" in line for line in payload["lines"])


def test_settings_api_exposes_values_and_source_users(server):
    client, _instance, _store = server
    status, payload = client.get_json("api/settings")
    assert status == 200
    assert payload["values"]["dry_run"] is True
    assert payload["accounts"] == ["main"]
    names = {user["name"] for user in payload["source"]["users"]}
    assert names == {"alice", "bob"}
    assert payload["source"]["episodes"] == 76
    assert payload["source_error"] is None


def test_settings_update_requires_header(server):
    client, _instance, _store = server
    status, payload = client.send("PUT", "api/settings", {"interval_seconds": 120}, header=False)
    assert status == 403

    status, payload = client.send("PUT", "api/settings", {
        "interval_seconds": 120,
        "dry_run": False,
        "users": [{"fnos_user": "alice", "account": "main", "enabled": True}],
    })
    assert status == 200
    assert payload["values"]["interval_seconds"] == 120
    assert payload["values"]["dry_run"] is False
    assert [user["fnos_user"] for user in payload["values"]["users"]] == ["alice"]


def test_settings_update_validation_problem(server):
    client, _instance, _store = server
    status, payload = client.send("PUT", "api/settings", {"interval_seconds": 1})
    assert status == 400
    assert "interval_seconds" in payload["error"]


def test_settings_reset(server):
    client, _instance, _store = server
    client.send("PUT", "api/settings", {"interval_seconds": 150})
    status, payload = client.send("POST", "api/settings/reset")
    assert status == 200
    assert payload["values"]["interval_seconds"] == 300


def test_source_users_endpoint(server):
    client, _instance, _store = server
    status, payload = client.get_json("api/source-users?refresh=1")
    assert status == 200
    assert payload["seasons"] == 2
    assert {user["name"] for user in payload["users"]} == {"alice", "bob"}


def test_ui_prefs_endpoint(server):
    client, _instance, _store = server
    status, payload = client.get_json("api/ui")
    assert status == 200
    assert payload["theme"] == "auto"
    assert payload["background"]["mode"] == "none"

    # write guard
    assert client.send("PUT", "api/ui", {"theme": "dark"}, header=False)[0] == 403
    status, payload = client.send("PUT", "api/ui", {"theme": "dark", "accent": "#c792ea"})
    assert status == 200
    assert payload["ui"]["theme"] == "dark"
    assert payload["ui"]["rev"] >= 1

    status, payload = client.send("PUT", "api/ui", {"accent": "red"})
    assert status == 400
    assert "accent" in payload["error"]

    status, payload = client.get_json("api/status")
    assert payload["ui"]["theme"] == "dark"


def test_background_upload_and_serving(server):
    client, _instance, _store = server
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 256

    with pytest.raises(urllib.error.HTTPError) as excinfo:
        client.get("static/background")
    assert excinfo.value.code == 404

    status, payload = client.send_raw("PUT", "api/ui/background", png, "image/png")
    assert status == 200
    background = payload["ui"]["background"]
    assert background["mode"] == "image"
    assert background["content_type"] == "image/png"
    url = background["url"]

    status, body = client.get(url.lstrip("/"))
    assert status == 200 and body == png

    status, payload = client.send("DELETE", "api/ui/background")
    assert status == 200
    assert payload["ui"]["background"]["mode"] == "none"
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        client.get("static/background")
    assert excinfo.value.code == 404


def test_background_rejects_non_images(server):
    client, _instance, _store = server
    status, payload = client.send_raw("PUT", "api/ui/background", b"hello", "image/png")
    assert status == 400
    assert "PNG" in payload["error"]


def test_palette_endpoint(server):
    client, _instance, _store = server
    status, payload = client.send("PUT", "api/ui/palette", {"colors": ["#aabbcc", "#112233"]})
    assert status == 200
    assert payload["ui"]["palette"] == ["#aabbcc", "#112233"]
    status, payload = client.send("PUT", "api/ui/palette", {"colors": []})
    assert status == 400


def test_background_preset(server):
    client, _instance, _store = server
    status, payload = client.send("PUT", "api/ui/background", {"preset": "dusk"})
    assert status == 200
    assert payload["ui"]["background"]["mode"] == "preset"
    assert payload["ui"]["background"]["preset"] == "dusk"
    status, payload = client.send("PUT", "api/ui/background", {"preset": "nope"})
    assert status == 400


def test_build_status_without_runtime_state(tmp_path, fnos_db):
    cfg = make_config(fnos_db, str(tmp_path / "state.db"))
    store = StateStore(cfg.app.state_db)
    try:
        data = build_status(cfg, store, None)
        assert data["last_result"] is None
        assert data["run_requested"] is False
        assert data["logs"] == []
        assert data["users"][0]["fnos_user"] == "alice"
    finally:
        store.close()

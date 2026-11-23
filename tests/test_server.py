from __future__ import annotations

import json
import socket
import time
import urllib.request

from conftest import make_config

from fnos2bgm.config import ServerConfig
from fnos2bgm.server import StatusServer, StatusState, build_status
from fnos2bgm.state import StateStore


def free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def test_status_page_and_api(tmp_path, fnos_db):
    cfg = make_config(fnos_db, str(tmp_path / "state.db"), dry_run=True)
    cfg.server = ServerConfig(enabled=True, listen="127.0.0.1:%d" % free_port())
    store = StateStore(cfg.app.state_db)
    store.save_mapping("main", "fnos:abc:s1", subject_id=1940, subject_name="\u9b54\u5361\u5c11\u5973\u6a31",
                       strategy="auto", confidence=1.0)
    store.save_candidates("main", "fnos:def:s1", {
        "title": "\u672a\u5339\u914d\u756a\u7ec4",
        "season": 1,
        "episode_count": 21,
        "reason": "best candidate 0.394 < min_score 0.860",
        "candidates": [{"subject_id": 148295, "name": "\u8336\u554a\u4e8c\u4e2d", "name_cn": "\u8336\u554a\u4e8c\u4e2d",
                        "year": 2014, "eps": 10, "score": 0.394}],
    })
    state = StatusState(started_at=int(time.time()) - 5, dry_run=True)
    server = StatusServer(cfg, store, state)
    server.start()
    try:
        page = urllib.request.urlopen(server.address, timeout=10).read().decode("utf-8")
        assert "fnos2bgm" in page
        assert "DRY-RUN" in page
        assert "\u9b54\u5361\u5c11\u5973\u6a31" in page
        assert "\u672a\u5339\u914d\u756a\u7ec4" in page
        assert "fnos2bgm map add" in page

        api = json.loads(urllib.request.urlopen(server.address + "api/status", timeout=10).read())
        assert api["dry_run"] is True
        assert api["stats"]["mappings"] == 1
        assert len(api["review"]) == 1
        assert api["mappings"][0]["subject_url"].endswith("/1940")

        health = urllib.request.urlopen(server.address + "healthz", timeout=10).read()
        assert b'"ok": true' in health or b'"ok":true' in health
    finally:
        server.stop()
        store.close()


def test_build_status_without_runtime_state(tmp_path, fnos_db):
    cfg = make_config(fnos_db, str(tmp_path / "state.db"))
    store = StateStore(cfg.app.state_db)
    try:
        data = build_status(cfg, store, None)
        assert data["last_result"] is None
        assert data["users"][0]["fnos_user"] == "alice"
        assert data["runs"] == []
    finally:
        store.close()

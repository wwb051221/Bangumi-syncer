"""Read-mostly web UI + JSON API served by the running sync process.

* ``/``                the SPA (vanilla JS, no build step)
* ``/api/status``      everything the UI renders, including a redacted config
* ``/api/logs``        in-memory ring buffer of recent log lines
* ``/api/search``      public Bangumi subject search (used by the mapping editor)
* ``POST /api/run``    ask the loop to sync right now
* ``POST /api/map``    create/override a mapping (manual strategy)
* ``DELETE /api/map``  remove a mapping (and the push history of that range)
* ``DELETE /api/candidates``  drop an item from the review queue

Write endpoints require the ``X-Requested-With: bangumi-syncer`` header, which a
cross-site HTML form cannot set; combined with no CORS headers that is enough
for a LAN-only tool. The page itself never displays tokens.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.parse
from collections import deque
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from . import __version__, trackers
from .config import Config
from .log import get_logger
from .state import StateStore
from .sync import RunResult
from .util import format_time, human_duration

log = get_logger("server")

STATIC_FILES = {
    "/static/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/static/style.css": ("style.css", "text/css; charset=utf-8"),
}
LOG_LIMIT = 500


class HttpError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class _RingHandler(logging.Handler):
    """Keep recent log lines in memory so the UI can show them."""

    def __init__(self, sink: deque[str]) -> None:
        super().__init__()
        self.sink = sink
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)-5s %(name)s %(message)s",
                                            datefmt="%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.sink.append(self.format(record))
        except Exception:  # pragma: no cover - logging must never raise
            pass


@dataclass
class StatusState:
    """Mutable information shared between the sync loop and the HTTP threads."""

    started_at: int
    dry_run: bool
    source_description: dict[str, Any] = field(default_factory=dict)
    last_result: RunResult | None = None
    next_run_at: int | None = None
    run_requested: threading.Event = field(default_factory=threading.Event)
    logs: deque[str] = field(default_factory=lambda: deque(maxlen=LOG_LIMIT))

    def log_lines(self, limit: int = 200) -> list[str]:
        lines = list(self.logs)
        return lines[-limit:] if limit > 0 else lines


def _json_response(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def build_status(cfg: Config, store: StateStore, state: StatusState | None = None) -> dict[str, Any]:
    tz = cfg.app.timezone
    runs = store.runs(10)
    for run in runs:
        run["started_at_human"] = format_time(run.get("started_at"), tz)
        if run.get("finished_at"):
            run["duration"] = human_duration((run["finished_at"] or 0) - (run["started_at"] or 0))

    mappings = []
    for row in store.mappings()[:200]:
        item = dict(row)
        item["subject_url"] = "https://bgm.tv/subject/%d" % item["subject_id"]
        mappings.append(item)

    review = []
    for row in store.candidates():
        payload = row.get("payload") or {}
        review.append({
            "account": row["account"],
            "series_key": row["series_key"],
            "title": payload.get("title", ""),
            "original_title": payload.get("original_title"),
            "season": payload.get("season"),
            "year": payload.get("year"),
            "episode_count": payload.get("episode_count"),
            "reason": payload.get("reason", ""),
            "candidates": (payload.get("candidates") or [])[:8],
            "updated_at": row["updated_at"],
        })

    accounts = store.accounts()
    for account in accounts:
        account["expires_in"] = max(0, int(account.get("expires_at") or 0) - int(time.time()))

    return {
        "version": __version__,
        "dry_run": cfg.app.dry_run,
        "timezone": tz,
        "uptime_seconds": int(time.time() - state.started_at) if state else 0,
        "next_run_at": state.next_run_at if state else None,
        "next_run_at_human": format_time(state.next_run_at if state else None, tz),
        "run_requested": bool(state.run_requested.is_set()) if state else False,
        "source": (state.source_description if state else {}) or {"type": cfg.source.type, "mode": cfg.source.mode},
        "users": [{"fnos_user": u.fnos_user, "account": u.account, "enabled": u.enabled} for u in cfg.users],
        "accounts": accounts,
        "stats": store.stats(),
        "runs": runs,
        "mappings": mappings,
        "review": review,
        "last_result": state.last_result.as_dict() if state and state.last_result else None,
        "logs": state.log_lines(200) if state else [],
        "config": cfg.redacted(),
    }


class _Handler(BaseHTTPRequestHandler):
    server_version = "bangumi-syncer/" + __version__

    cfg: Config
    store: StateStore
    state: StatusState | None
    assets: dict[str, bytes]
    searchers: dict[str, Any]
    search_lock: threading.Lock

    # ------------------------------------------------------------- transport
    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        try:
            self._route(method)
        except HttpError as exc:
            self._send_json(exc.status, {"error": exc.message})
        except (BrokenPipeError, ConnectionResetError):  # pragma: no cover
            pass
        except Exception as exc:  # pragma: no cover - unexpected
            log.exception("unhandled error for %s %s", method, self.path)
            self._send_json(500, {"error": "%s: %s" % (type(exc).__name__, exc)})

    def _route(self, method: str) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = urllib.parse.parse_qs(parsed.query)

        if method == "GET":
            if path in ("/", "/index.html"):
                self._send_bytes(200, "text/html; charset=utf-8", self.assets["index.html"])
                return
            if path in STATIC_FILES:
                key, content_type = STATIC_FILES[path]
                self._send_bytes(200, content_type, self.assets[key], cache_seconds=300)
                return
            if path == "/favicon.ico":
                self._send_bytes(204, "image/x-icon", b"")
                return
            if path == "/healthz":
                self._send_json(200, {"ok": True, "version": __version__})
                return
            if path == "/api/status":
                self._send_json(200, build_status(self.cfg, self.store, self.state))
                return
            if path == "/api/logs":
                limit = _int_param(query, "limit", 200, 1, 1000)
                lines = self.state.log_lines(limit) if self.state else []
                self._send_json(200, {"lines": lines, "run_requested":
                                      bool(self.state.run_requested.is_set()) if self.state else False})
                return
            if path == "/api/search":
                self._handle_search(query)
                return
            raise HttpError(404, "not found")

        self._require_write_header()
        if method == "POST" and path == "/api/run":
            self._handle_run()
            return
        if method == "POST" and path == "/api/map":
            self._handle_map_create()
            return
        if method == "DELETE" and path == "/api/map":
            self._handle_map_delete(query)
            return
        if method == "DELETE" and path == "/api/candidates":
            self._handle_candidate_delete(query)
            return
        raise HttpError(404, "not found")

    # ---------------------------------------------------------------- helpers
    def _require_write_header(self) -> None:
        if self.headers.get("X-Requested-With") != "bangumi-syncer":
            raise HttpError(403, "write operations require the X-Requested-With header")

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError as exc:
            raise HttpError(400, "invalid Content-Length") from exc
        if length <= 0:
            raise HttpError(400, "empty request body")
        if length > 65536:
            raise HttpError(413, "request body too large")
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HttpError(400, "invalid JSON body") from exc
        if not isinstance(payload, dict):
            raise HttpError(400, "expected a JSON object")
        return payload

    # ------------------------------------------------------------------ APIs
    def _handle_run(self) -> None:
        if self.state is None:
            raise HttpError(503, "the sync loop is not running in this process")
        if self.state.run_requested.is_set():
            raise HttpError(409, "a sync run is already queued")
        self.state.run_requested.set()
        log.info("sync run requested from the web UI")
        self._send_json(202, {"queued": True})

    def _handle_search(self, query: dict[str, list[str]]) -> None:
        term = (query.get("q") or [""])[0].strip()
        if not term:
            raise HttpError(400, "q is required")
        limit = _int_param(query, "limit", 8, 1, 20)
        account_name = (query.get("account") or [""])[0] or self.cfg.target.accounts[0].name
        tracker = self._tracker(account_name)
        try:
            hits = tracker.search(term, limit, self.cfg.match.accept_types)
        except trackers.TrackerError as exc:
            raise HttpError(502, "Bangumi search failed: %s" % exc) from exc
        self._send_json(200, {"query": term, "results": [
            {"subject_id": hit.subject_id, "name": hit.name, "name_cn": hit.name_cn,
             "year": hit.year, "eps": hit.episode_count}
            for hit in hits
        ]})

    def _tracker(self, account_name: str):
        with self.search_lock:
            tracker = self.searchers.get(account_name)
            if tracker is None:
                tracker = trackers.create(self.cfg.account(account_name), self.cfg.target, self.store)
                self.searchers[account_name] = tracker
            return tracker

    def _handle_map_create(self) -> None:
        body = self._read_json()
        account = str(body.get("account") or "")
        series_key = str(body.get("series_key") or "").strip()
        subject_id = _as_int(body.get("subject_id"))
        ep_from = _as_int(body.get("ep_from") if body.get("ep_from") is not None else 1)
        ep_to = _as_int(body.get("ep_to") if body.get("ep_to") is not None else 9999)
        name = str(body.get("name") or "").strip()

        if account not in [a.name for a in self.cfg.target.accounts]:
            raise HttpError(400, "unknown account %r" % account)
        if not series_key:
            raise HttpError(400, "series_key is required")
        if subject_id is None or subject_id <= 0:
            raise HttpError(400, "subject_id must be a positive integer")
        if ep_from is None or ep_to is None or ep_from < 1 or ep_to < ep_from or ep_to > 99999:
            raise HttpError(400, "invalid episode range")

        if not name:
            try:
                name = self._tracker(account).subject_name(subject_id) or ""
            except trackers.TrackerError:
                name = ""

        self.store.save_mapping(
            account, series_key, subject_id=subject_id, subject_name=name,
            strategy="manual", confidence=1.0, ep_from=ep_from, ep_to=ep_to, confirmed=True,
        )
        cleared = self.store.delete_pushes(account, series_key, ep_from, ep_to)
        self.store.delete_candidates(account, series_key)
        log.info("mapping saved from the web UI: %s -> %d [%d-%d]", series_key, subject_id, ep_from, ep_to)
        self._send_json(200, {"ok": True, "series_key": series_key, "subject_id": subject_id,
                              "ep_from": ep_from, "ep_to": ep_to, "subject_name": name,
                              "cleared_pushes": cleared})

    def _handle_map_delete(self, query: dict[str, list[str]]) -> None:
        account = (query.get("account") or [""])[0]
        series_key = (query.get("series_key") or [""])[0]
        ep_from = _optional_int((query.get("ep_from") or [None])[0])
        if account not in [a.name for a in self.cfg.target.accounts]:
            raise HttpError(400, "unknown account %r" % account)
        if not series_key:
            raise HttpError(400, "series_key is required")

        target = None
        if ep_from is not None:
            for row in self.store.mapping_groups(account, series_key):
                if row["ep_from"] == ep_from:
                    target = row
                    break
        removed = self.store.delete_mapping(account, series_key, ep_from)
        if target is not None:
            cleared = self.store.delete_pushes(account, series_key, int(target["ep_from"]), int(target["ep_to"]))
        elif ep_from is None:
            cleared = self.store.delete_pushes(account, series_key)
        else:
            cleared = 0
        log.info("mapping removed from the web UI: %s (%d row(s))", series_key, removed)
        self._send_json(200, {"ok": True, "removed": removed, "cleared_pushes": cleared})

    def _handle_candidate_delete(self, query: dict[str, list[str]]) -> None:
        account = (query.get("account") or [""])[0]
        series_key = (query.get("series_key") or [""])[0]
        if not account or not series_key:
            raise HttpError(400, "account and series_key are required")
        self.store.delete_candidates(account, series_key)
        self._send_json(200, {"ok": True})

    # --------------------------------------------------------------- responses
    def _send_json(self, status: int, payload: Any) -> None:
        self._send_bytes(status, "application/json; charset=utf-8", _json_response(payload))

    def _send_bytes(self, status: int, content_type: str, body: bytes,
                    cache_seconds: int = 0) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store" if cache_seconds == 0
                             else "public, max-age=%d" % cache_seconds)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if body:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):  # pragma: no cover
            pass

    def log_message(self, fmt: str, *args) -> None:
        log.debug("http %s", fmt % args)


def _int_param(query: dict[str, list[str]], name: str, default: int, low: int, high: int) -> int:
    raw = (query.get(name) or [""])[0]
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(low, min(high, value))


def _optional_int(value: str | None) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _as_int(value: Any) -> int | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class StatusServer:
    def __init__(self, cfg: Config, store: StateStore, state: StatusState | None = None) -> None:
        self.cfg = cfg
        self.store = store
        self.state = state
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._ring: _RingHandler | None = None
        self._assets = _load_assets()

    @property
    def address(self) -> str:
        host, port = self.cfg.server.host_port()
        return "http://%s:%d/" % ("127.0.0.1" if host in ("0.0.0.0", "::") else host, port)

    def start(self) -> None:
        host, port = self.cfg.server.host_port()
        handler = type(
            "BoundHandler",
            (_Handler,),
            {
                "cfg": self.cfg,
                "store": self.store,
                "state": self.state,
                "assets": self._assets,
                "searchers": {},
                "search_lock": threading.Lock(),
            },
        )
        self._server = ThreadingHTTPServer((host, port), handler)
        self._server.daemon_threads = True
        if self.state is not None:
            self._ring = _RingHandler(self.state.logs)
            self._ring.setLevel(logging.INFO)
            logging.getLogger().addHandler(self._ring)
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        kwargs={"poll_interval": 0.5},
                                        daemon=True, name="status-server")
        self._thread.start()
        log.info("web UI on %s", self.address)

    def stop(self) -> None:
        if self._ring is not None:
            logging.getLogger().removeHandler(self._ring)
            self._ring = None
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None


def _load_assets() -> dict[str, bytes]:
    from importlib import resources

    assets: dict[str, bytes] = {}
    package = resources.files("bangumi_syncer.web")
    for name in ("index.html", "app.js", "style.css"):
        assets[name] = package.joinpath(name).read_bytes()
    return assets

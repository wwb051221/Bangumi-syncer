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
import secrets
import threading
import time
import urllib.parse
from collections import deque
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from . import __version__, oauth, trackers
from .auth import AuthError, AuthManager
from .config import Config
from .http import HttpClient
from .log import get_logger
from .oauth import OAuthError
from .settings import SettingsError, SettingsManager
from .state import StateStore
from .sync import RunResult
from .ui import MAX_IMAGE_BYTES, UiError, UiManager
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


def build_status(cfg: Config, store: StateStore, state: StatusState | None = None,
                 ui: UiManager | None = None) -> dict[str, Any]:
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
        "ui": ui.public() if ui else None,
    }


class _Handler(BaseHTTPRequestHandler):
    server_version = "bangumi-syncer/" + __version__

    cfg: Config
    store: StateStore
    state: StatusState | None
    settings: SettingsManager | None
    ui: UiManager | None
    assets: dict[str, bytes]
    searchers: dict[str, Any]
    search_lock: threading.Lock

    # ------------------------------------------------------------- transport
    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch("PUT")

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

        self._user = self.auth.user_from_cookie(self.headers.get("Cookie"))
        setup_required = self.auth.setup_required()
        public_get = method == "GET" and (
            path in ("/", "/index.html", "/favicon.ico", "/healthz", "/api/auth", "/static/background")
            or path in STATIC_FILES
        )
        public_post = method == "POST" and path in ("/api/auth/login", "/api/auth/setup", "/api/auth/logout")
        if setup_required:
            if not (public_get or public_post):
                raise HttpError(401, "setup required")
        elif not self._user:
            if not (public_get or (method == "POST" and path == "/api/auth/login")):
                raise HttpError(401, "unauthorized")
        if path.startswith("/api/users") and (self._user or {}).get("role") != "admin":
            raise HttpError(403, "需要管理员权限")

        if method == "GET":
            if path in ("/", "/index.html"):
                self._send_bytes(200, "text/html; charset=utf-8", self.assets["index.html"])
                return
            if path in STATIC_FILES:
                key, content_type = STATIC_FILES[path]
                # HTML/JS/CSS must never be stale after a restart; the
                # background image keeps its cache because it is versioned.
                cache = 0 if key in ("app.js", "style.css") else 300
                self._send_bytes(200, content_type, self.assets[key], cache_seconds=cache)
                return
            if path == "/favicon.ico":
                self._send_bytes(204, "image/x-icon", b"")
                return
            if path == "/healthz":
                self._send_json(200, {"ok": True, "version": __version__})
                return
            if path == "/api/status":
                self._send_json(200, build_status(self.cfg, self.store, self.state, self.ui))
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
            if path == "/api/settings":
                self._handle_settings_get()
                return
            if path == "/api/source-users":
                self._handle_source_users(query)
                return
            if path == "/api/oauth":
                self._send_json(200, self._oauth_status())
                return
            if path == "/api/auth":
                self._handle_auth_status()
                return
            if path == "/api/users":
                self._handle_users_list()
                return
            if path == "/api/ui":
                self._handle_ui_get()
                return
            if path == "/static/background":
                self._serve_background()
                return
            raise HttpError(404, "not found")

        self._require_write_header()
        if method == "POST" and path == "/api/run":
            self._handle_run()
            return
        if method == "POST" and path == "/api/map":
            self._handle_map_create()
            return
        if method == "PUT" and path == "/api/settings":
            self._handle_settings_update()
            return
        if method == "PUT" and path == "/api/oauth":
            self._handle_oauth_update()
            return
        if method == "POST" and path == "/api/oauth/start":
            self._handle_oauth_start()
            return
        if method == "POST" and path == "/api/oauth/exchange":
            self._handle_oauth_exchange()
            return
        if method == "POST" and path == "/api/auth/setup":
            self._handle_auth_setup()
            return
        if method == "POST" and path == "/api/auth/login":
            self._handle_auth_login()
            return
        if method == "POST" and path == "/api/auth/logout":
            self._handle_auth_logout()
            return
        if method == "POST" and path == "/api/auth/password":
            self._handle_auth_password()
            return
        if method == "POST" and path == "/api/users":
            self._handle_users_create()
            return
        if method == "POST" and path == "/api/users/reset":
            self._handle_users_reset()
            return
        if method == "DELETE" and path == "/api/users":
            self._handle_users_delete(query)
            return
        if method == "POST" and path == "/api/settings/reset":
            self._handle_settings_reset()
            return
        if method == "PUT" and path == "/api/ui":
            self._handle_ui_update()
            return
        if method == "PUT" and path == "/api/ui/palette":
            self._handle_ui_palette()
            return
        if method == "PUT" and path == "/api/ui/background":
            self._handle_background_put()
            return
        if method == "DELETE" and path == "/api/ui/background":
            self._handle_background_delete()
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
        if self.headers.get("X-Requested-With") == "bangumi-syncer":
            return
        # Drain the body before rejecting, otherwise some clients see a reset
        # instead of the 403 (the socket closes with unread data).
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if 0 < length <= 65536:
                self.rfile.read(length)
        except (ValueError, OSError):
            pass
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

    # -------------------------------------------------------------- settings
    def _settings_manager(self):
        if self.settings is None:
            raise HttpError(503, "settings are not available in this process")
        return self.settings

    def _settings_payload(self, manager) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "values": manager.values(),
            "accounts": manager.accounts,
        }
        try:
            payload["source"] = manager.source_users()
            payload["source_error"] = None
        except Exception as exc:  # noqa: BLE001 - show the reason in the UI
            payload["source"] = {"users": [], "seasons": 0, "episodes": 0}
            payload["source_error"] = str(exc)
        return payload

    def _handle_settings_get(self) -> None:
        manager = self._settings_manager()
        self._send_json(200, self._settings_payload(manager))

    def _handle_source_users(self, query: dict[str, list[str]]) -> None:
        manager = self._settings_manager()
        force = (query.get("refresh") or [""])[0] in ("1", "true", "yes")
        try:
            self._send_json(200, manager.source_users(force=force))
        except Exception as exc:  # noqa: BLE001
            raise HttpError(503, str(exc)) from exc

    def _handle_settings_update(self) -> None:
        manager = self._settings_manager()
        body = self._read_json()
        try:
            values = manager.update(body)
        except SettingsError as exc:
            raise HttpError(400, str(exc)) from exc
        self._send_json(200, {"ok": True, "values": values, "accounts": manager.accounts})

    def _handle_settings_reset(self) -> None:
        manager = self._settings_manager()
        self._send_json(200, {"ok": True, "values": manager.reset(), "accounts": manager.accounts})

    # --------------------------------------------------------- auth / users
    def _auth_payload(self) -> dict[str, Any]:
        user = self._user or {}
        return {
            "setup_required": self.auth.setup_required(),
            "authenticated": bool(self._user),
            "username": user.get("username"),
            "role": user.get("role"),
        }

    def _handle_auth_status(self) -> None:
        self._send_json(200, self._auth_payload())

    def _auth_body(self) -> dict[str, Any]:
        body = self._read_json()
        if not isinstance(body, dict):
            raise HttpError(400, "expected a JSON object")
        return body

    def _handle_auth_setup(self) -> None:
        body = self._auth_body()
        username = str(body.get("username") or "")
        password = str(body.get("password") or "")
        confirm = str(body.get("confirm") or password)
        if password != confirm:
            raise HttpError(400, "两次输入的密码不一致")
        try:
            result = self.auth.setup(username, password)
        except AuthError as exc:
            raise HttpError(400, str(exc)) from exc
        self._user = {"username": result["username"], "role": result["role"]}
        self._send_json(200, {"ok": True, "auth": self._auth_payload()},
                        cookies=[self.auth.issue_cookie(result["username"])])
        log.info("initial web administrator %r created", result["username"])

    def _handle_auth_login(self) -> None:
        body = self._auth_body()
        username = str(body.get("username") or "").strip()
        password = str(body.get("password") or "")
        if self.auth.setup_required():
            raise HttpError(409, "尚未初始化管理员账号")
        if not self.auth.verify(username, password):
            log.warning("failed web login for %r", username)
            raise HttpError(401, "用户名或密码不正确")
        user = self.auth.get_user(username) or {}
        role = user.get("role") or "member"
        self._user = {"username": username, "role": role}
        self._send_json(200, {"ok": True, "auth": self._auth_payload()},
                        cookies=[self.auth.issue_cookie(username)])
        log.info("web login: %s (%s)", username, role)

    def _handle_auth_logout(self) -> None:
        self._user = None
        self._send_json(200, {"ok": True}, cookies=[self.auth.clear_cookie()])

    def _handle_auth_password(self) -> None:
        if not self._user:
            raise HttpError(401, "unauthorized")
        body = self._auth_body()
        current = str(body.get("current_password") or "")
        new = str(body.get("new_password") or "")
        confirm = str(body.get("confirm") or new)
        if new != confirm:
            raise HttpError(400, "两次输入的新密码不一致")
        try:
            self.auth.change_password(self._user["username"], current, new)
        except AuthError as exc:
            raise HttpError(400, str(exc)) from exc
        self._send_json(200, {"ok": True})

    def _handle_users_list(self) -> None:
        self._send_json(200, {"users": self.auth.list_users()})

    def _handle_users_create(self) -> None:
        body = self._auth_body()
        try:
            user = self.auth.create_user(
                str(body.get("username") or ""),
                str(body.get("password") or ""),
                str(body.get("role") or "member"),
            )
        except AuthError as exc:
            raise HttpError(400, str(exc)) from exc
        self._send_json(200, {"ok": True, "user": user, "users": self.auth.list_users()})

    def _handle_users_reset(self) -> None:
        body = self._auth_body()
        username = str(body.get("username") or "")
        password = body.get("password")
        role = body.get("role")
        try:
            if password:
                self.auth.reset_password(username, str(password))
            if role:
                self.auth.set_role(username, str(role))
        except AuthError as exc:
            raise HttpError(400, str(exc)) from exc
        self._send_json(200, {"ok": True, "users": self.auth.list_users()})

    def _handle_users_delete(self, query: dict[str, list[str]]) -> None:
        name = (query.get("name") or [""])[0]
        actor = (self._user or {}).get("username") or ""
        try:
            self.auth.delete_user(name, actor)
        except AuthError as exc:
            raise HttpError(400, str(exc)) from exc
        self._send_json(200, {"ok": True, "users": self.auth.list_users()})

    # ------------------------------------------------------------------ oauth
    def _oauth_account(self, name: object):
        accounts = self.cfg.target.accounts
        if not accounts:
            raise HttpError(400, "没有配置 Bangumi 账号")
        wanted = str(name or accounts[0].name)
        for account in accounts:
            if account.name == wanted:
                return account
        raise HttpError(400, "未知账号：%s" % wanted)

    def _oauth_status(self) -> dict[str, Any]:
        accounts = []
        for account in self.cfg.target.accounts:
            saved = oauth.stored_client(self.store, account.name)
            tokens = self.store.tokens(account.name)
            accounts.append({
                "name": account.name,
                "client_id": saved.get("client_id") or account.client_id,
                "has_secret": bool(saved.get("client_secret") or account.client_secret),
                "redirect_uri": saved.get("redirect_uri") or account.redirect_uri,
                "has_token": bool(tokens),
                "expires_at": tokens.expires_at if tokens else None,
                "remote_user": tokens.remote_user if tokens else None,
            })
        return {"accounts": accounts}

    def _handle_oauth_update(self) -> None:
        body = self._read_json()
        account = self._oauth_account(body.get("account"))
        client_id = str(body.get("client_id") or "").strip()
        redirect_uri = str(body.get("redirect_uri") or "").strip()
        secret = body.get("client_secret")
        secret = str(secret).strip() if isinstance(secret, str) else ""
        if not client_id:
            raise HttpError(400, "App ID 不能为空")
        if redirect_uri and not redirect_uri.startswith(("http://", "https://")):
            raise HttpError(400, "回调地址必须是 http(s) URL")
        oauth.save_client(
            self.store,
            account.name,
            client_id=client_id,
            client_secret=secret or None,
            redirect_uri=redirect_uri or None,
        )
        oauth.apply_stored_credentials(self.cfg, self.store)
        log.info("oauth app credentials updated for %r", account.name)
        self._send_json(200, {"ok": True, "oauth": self._oauth_status()})

    def _handle_oauth_start(self) -> None:
        body = self._read_json()
        account = self._oauth_account(body.get("account"))
        if not account.client_id or not account.client_secret:
            raise HttpError(400, "请先填写并保存 App ID / App Secret")
        state = secrets.token_urlsafe(16)
        self._send_json(200, {"ok": True, "url": oauth.authorize_url(account, state)})

    def _handle_oauth_exchange(self) -> None:
        body = self._read_json()
        account = self._oauth_account(body.get("account"))
        code = oauth.extract_code(str(body.get("code") or ""))
        if not code:
            raise HttpError(400, "请粘贴回调地址或 code")
        http = HttpClient(
            user_agent=self.cfg.target.user_agent,
            timeout=self.cfg.target.timeout_seconds,
            max_retries=self.cfg.target.max_retries,
            min_interval=0,
        )
        try:
            tokens = oauth.exchange_code(account, code, http)
        except OAuthError as exc:
            raise HttpError(400, str(exc)) from exc
        remote = oauth.remote_user(http, tokens)
        if remote:
            tokens.remote_user = remote
        self.store.save_tokens(account.name, tokens)
        log.info("oauth tokens stored for %r", account.name)
        self._send_json(200, {"ok": True, "oauth": self._oauth_status()})

    # ------------------------------------------------------------- appearance
    def _ui_manager(self) -> UiManager:
        if self.ui is None:
            raise HttpError(503, "UI personalisation is not available in this process")
        return self.ui

    def _handle_ui_get(self) -> None:
        self._send_json(200, self._ui_manager().public())

    def _handle_ui_update(self) -> None:
        manager = self._ui_manager()
        body = self._read_json()
        try:
            self._send_json(200, {"ok": True, "ui": manager.update(body)})
        except UiError as exc:
            raise HttpError(400, str(exc)) from exc

    def _handle_ui_palette(self) -> None:
        manager = self._ui_manager()
        body = self._read_json()
        try:
            self._send_json(200, {"ok": True, "ui": manager.set_palette(body.get("colors"))})
        except UiError as exc:
            raise HttpError(400, str(exc)) from exc

    def _handle_background_put(self) -> None:
        manager = self._ui_manager()
        content_type = self.headers.get("Content-Type") or ""
        try:
            if content_type.startswith("image/"):
                data = self._read_raw(MAX_IMAGE_BYTES + 1)
                ui = manager.set_background_bytes(data, content_type)
            else:
                body = self._read_json()
                if body.get("url"):
                    ui = manager.set_background_url(str(body["url"]))
                elif body.get("preset"):
                    ui = manager.use_preset(str(body["preset"]))
                else:
                    raise HttpError(400, "expected an image body, a url or a preset")
        except UiError as exc:
            raise HttpError(400, str(exc)) from exc
        self._send_json(200, {"ok": True, "ui": ui})

    def _handle_background_delete(self) -> None:
        manager = self._ui_manager()
        try:
            ui = manager.clear_background()
        except UiError as exc:
            raise HttpError(400, str(exc)) from exc
        self._send_json(200, {"ok": True, "ui": ui})

    def _serve_background(self) -> None:
        manager = self.ui
        item = manager.background() if manager is not None else None
        if item is None:
            raise HttpError(404, "no background image")
        data, content_type, version = item
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=3600")
        self.send_header("ETag", 'W/"bg-%d"' % version)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _read_raw(self, limit: int) -> bytes:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError as exc:
            raise HttpError(400, "invalid Content-Length") from exc
        if length <= 0:
            raise HttpError(400, "empty request body")
        if length > limit:
            raise HttpError(413, "image is too large")
        return self.rfile.read(length)

    # --------------------------------------------------------------- responses
    def _send_json(self, status: int, payload: Any, cookies: list[str] | None = None) -> None:
        headers = [("Set-Cookie", cookie) for cookie in (cookies or [])]
        self._send_bytes(status, "application/json; charset=utf-8",
                         _json_response(payload), extra_headers=headers)

    def _send_bytes(self, status: int, content_type: str, body: bytes,
                    cache_seconds: int = 0, extra_headers: list[tuple[str, str]] | None = None) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store" if cache_seconds == 0
                             else "public, max-age=%d" % cache_seconds)
            self.send_header("X-Content-Type-Options", "nosniff")
            for name, value in extra_headers or []:
                self.send_header(name, value)
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
    def __init__(
        self,
        cfg: Config,
        store: StateStore,
        state: StatusState | None = None,
        settings: SettingsManager | None = None,
        ui: UiManager | None = None,
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.state = state
        self.settings = settings
        self.ui = ui
        self.auth = AuthManager(store)
        self._user: dict[str, Any] | None = None
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
                "settings": self.settings,
                "ui": self.ui,
                "auth": self.auth,
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

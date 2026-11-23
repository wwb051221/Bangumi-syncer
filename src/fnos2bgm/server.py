"""Read-only status page and JSON API.

Deliberately read-only: all mutations go through the CLI (which is easy to
audit and to run over SSH). The page shows what the last run did, which shows
are mapped and which ones still need a human decision.
"""

from __future__ import annotations

import html
import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from . import __version__
from .config import Config
from .log import get_logger
from .state import StateStore
from .sync import RunResult
from .util import format_time, human_duration

log = get_logger("server")


@dataclass
class StatusState:
    """Mutable run information shared between the loop and the HTTP thread."""

    started_at: int
    dry_run: bool
    source_description: dict[str, Any] = field(default_factory=dict)
    last_result: RunResult | None = None
    next_run_at: int | None = None


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
        review.append(
            {
                "account": row["account"],
                "series_key": row["series_key"],
                "title": payload.get("title", ""),
                "original_title": payload.get("original_title"),
                "season": payload.get("season"),
                "year": payload.get("year"),
                "episode_count": payload.get("episode_count"),
                "reason": payload.get("reason", ""),
                "candidates": (payload.get("candidates") or [])[:5],
                "updated_at": row["updated_at"],
            }
        )
    payload = {
        "version": __version__,
        "dry_run": cfg.app.dry_run,
        "timezone": tz,
        "uptime_seconds": int(__import__("time").time() - state.started_at) if state else 0,
        "next_run_at": state.next_run_at if state else None,
        "next_run_at_human": format_time(state.next_run_at if state else None, tz),
        "source": (state.source_description if state else {}) or {"type": cfg.source.type, "mode": cfg.source.mode},
        "users": [{"fnos_user": u.fnos_user, "account": u.account, "enabled": u.enabled} for u in cfg.users],
        "accounts": store.accounts(),
        "stats": store.stats(),
        "runs": runs,
        "mappings": mappings,
        "review": review,
        "last_result": state.last_result.as_dict() if state and state.last_result else None,
    }
    return payload


STYLE = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { font-family: system-ui, -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
       margin: 0; padding: 1.5rem; line-height: 1.5; background: #0f1115; color: #e6e8ee; }
h1 { font-size: 1.35rem; margin: 0 0 .25rem; }
h2 { font-size: 1rem; margin: 1.75rem 0 .6rem; color: #9fb2d0; text-transform: uppercase; letter-spacing: .08em; }
a { color: #7db3ff; text-decoration: none; }
a:hover { text-decoration: underline; }
.muted { color: #8b95a7; }
.badge { display: inline-block; padding: .1rem .5rem; border-radius: 999px; font-size: .78rem; font-weight: 600; }
.badge.ok { background: #123524; color: #6ee7a8; }
.badge.warn { background: #3a2a10; color: #f5c26b; }
.badge.dry { background: #1b2b45; color: #9fc5ff; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(10rem, 1fr)); gap: .75rem; margin-top: 1rem; }
.card { background: #171a21; border: 1px solid #242a35; border-radius: .6rem; padding: .8rem 1rem; }
.card .n { font-size: 1.6rem; font-weight: 700; }
.card .l { color: #8b95a7; font-size: .8rem; text-transform: uppercase; letter-spacing: .05em; }
table { width: 100%; border-collapse: collapse; background: #171a21; border: 1px solid #242a35;
        border-radius: .6rem; overflow: hidden; font-size: .88rem; }
th, td { text-align: left; padding: .5rem .7rem; border-bottom: 1px solid #222833; vertical-align: top; }
th { background: #1b1f27; color: #9fb2d0; font-weight: 600; font-size: .78rem; text-transform: uppercase; }
tr:last-child td { border-bottom: none; }
code, pre { font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: .82rem; }
pre { background: #0b0d11; border: 1px solid #222833; border-radius: .4rem; padding: .5rem .7rem; overflow-x: auto; }
.empty { color: #6c7788; font-style: italic; padding: .6rem 0; }
"""


def _render(cfg: Config, store: StateStore, state: StatusState | None) -> str:
    data = build_status(cfg, store, state)
    tz = cfg.app.timezone
    e = html.escape
    parts: list[str] = []
    parts.append("<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">")
    parts.append("<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">")
    parts.append("<meta http-equiv=\"refresh\" content=\"15\">")
    parts.append("<title>fnos2bgm</title><style>" + STYLE + "</style></head><body>")

    badge = '<span class="badge dry">DRY-RUN</span>' if data["dry_run"] else '<span class="badge ok">LIVE</span>'
    parts.append("<h1>fnos2bgm <span class=\"muted\">v%s</span> %s</h1>" % (e(data["version"]), badge))
    parts.append(
        '<div class="muted">飞牛影视 → Bangumi 追番进度同步 · uptime %s · next run %s · '
        '<a href="/api/status">JSON</a> · <a href="/healthz">health</a></div>'
        % (human_duration(data["uptime_seconds"]), e(data["next_run_at_human"]))
    )

    stats = data["stats"]
    last = data["runs"][0] if data["runs"] else None
    status_badge = "<span class=\"badge ok\">last run ok</span>"
    if last and not last.get("ok"):
        status_badge = "<span class=\"badge warn\">last run failed</span>"
    parts.append('<div class="cards">')
    for label, value in (
        ("mappings", stats["mappings"]),
        ("episodes pushed", stats["pushed"]),
        ("failed pushes", stats["failed"]),
        ("need review", len(data["review"])),
    ):
        parts.append('<div class="card"><div class="n">%s</div><div class="l">%s</div></div>' % (value, label))
    parts.append("</div>")
    parts.append('<div style="margin-top:.75rem">%s</div>' % status_badge)

    # users
    parts.append("<h2>Users</h2><table><tr><th>fnOS user</th><th>Bangumi account</th><th>enabled</th></tr>")
    for user in data["users"]:
        parts.append(
            "<tr><td>%s</td><td>%s</td><td>%s</td></tr>"
            % (e(user["fnos_user"]), e(user["account"]), "yes" if user["enabled"] else "no")
        )
    if not data["users"]:
        parts.append('<tr><td colspan="3" class="empty">no users configured</td></tr>')
    parts.append("</table>")

    # accounts
    parts.append("<h2>Accounts</h2><table><tr><th>account</th><th>token</th><th>expires</th><th>bangumi user</th></tr>")
    for account in data["accounts"]:
        token = '<span class="badge ok">stored</span>' if account.get("has_token") else '<span class="badge warn">missing</span>'
        parts.append(
            "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
            % (e(account["name"]), token, e(format_time(account.get("expires_at"), tz)), e(account.get("remote_user") or "-"))
        )
    if not data["accounts"]:
        parts.append('<tr><td colspan="4" class="empty">run <code>fnos2bgm oauth --account NAME</code> first</td></tr>')
    parts.append("</table>")

    # runs
    parts.append("<h2>Recent runs</h2><table><tr><th>started</th><th>ok</th><th>pushed</th><th>failed</th>"
                 "<th>matched</th><th>unmatched</th><th>duration</th><th>message</th></tr>")
    for run in data["runs"]:
        parts.append(
            "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
            % (
                e(run.get("started_at_human") or "-"),
                "ok" if run.get("ok") else "fail",
                run.get("pushed") or 0,
                run.get("failed") or 0,
                run.get("matched") or 0,
                run.get("unmatched") or 0,
                e(run.get("duration") or "-"),
                e(run.get("message") or ""),
            )
        )
    if not data["runs"]:
        parts.append('<tr><td colspan="8" class="empty">no runs yet</td></tr>')
    parts.append("</table>")

    # review queue
    parts.append("<h2>Needs review (%d)</h2>" % len(data["review"]))
    if not data["review"]:
        parts.append('<div class="empty">nothing waiting for a decision 🎉</div>')
    for item in data["review"]:
        parts.append('<div class="card" style="margin-bottom:.75rem">')
        parts.append(
            "<div><strong>%s</strong> <span class=\"muted\">S%s · %s · %s eps · %s</span></div>"
            % (
                e(item["title"] or item["series_key"]),
                e(str(item.get("season") or "?")),
                e(str(item.get("year") or "?")),
                e(str(item.get("episode_count") or "?")),
                e(item["reason"]),
            )
        )
        parts.append('<div class="muted"><code>%s</code></div>' % e(item["series_key"]))
        if item["candidates"]:
            parts.append("<table style=\"margin-top:.5rem\"><tr><th>subject</th><th>score</th><th>name</th><th>year</th><th>eps</th></tr>")
            for cand in item["candidates"]:
                parts.append(
                    '<tr><td><a href="https://bgm.tv/subject/%d">%d</a></td><td>%s</td><td>%s</td>'
                    "<td>%s</td><td>%s</td></tr>"
                    % (
                        cand["subject_id"],
                        cand["subject_id"],
                        cand.get("score", ""),
                        e(cand.get("name_cn") or cand.get("name") or ""),
                        e(str(cand.get("year") or "")),
                        e(str(cand.get("eps") or "")),
                    )
                )
            parts.append("</table>")
            first = item["candidates"][0]["subject_id"]
        else:
            first = 0
        parts.append(
            "<pre>fnos2bgm map add --account %s --series '%s' --subject %s%s</pre>"
            % (e(item["account"]), e(item["series_key"]), first or "<id>",
               "" if not item.get("episode_count") else "")
        )
        parts.append("</div>")

    # mappings
    parts.append("<h2>Mappings (%d)</h2><table><tr><th>account</th><th>series key</th><th>local eps</th>"
                 "<th>subject</th><th>name</th><th>strategy</th><th>score</th><th>pushed</th></tr>")
    for mapping in data["mappings"]:
        parts.append(
            "<tr><td>%s</td><td><code>%s</code></td><td>%s–%s</td>"
            "<td><a href=\"%s\">%d</a></td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
            % (
                e(mapping["account"]),
                e(mapping["series_key"]),
                mapping["ep_from"],
                mapping["ep_to"],
                e(mapping["subject_url"]),
                mapping["subject_id"],
                e(mapping.get("subject_name") or ""),
                e(mapping.get("strategy") or ""),
                mapping.get("confidence") or "",
                mapping.get("pushed") or 0,
            )
        )
    if not data["mappings"]:
        parts.append('<tr><td colspan="8" class="empty">no mappings yet</td></tr>')
    parts.append("</table>")

    parts.append('<p class="muted" style="margin-top:2rem">generated %s · dry-run=%s</p>'
                 % (e(format_time(__import__("time").time(), tz)), data["dry_run"]))
    parts.append("</body></html>")
    return "".join(parts)


class _Handler(BaseHTTPRequestHandler):
    server_version = "fnos2bgm/" + __version__

    cfg: Config
    store: StateStore
    state: StatusState | None

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/healthz":
            self._send(200, "application/json", json.dumps({"ok": True, "version": __version__}).encode())
        elif path == "/api/status":
            payload = build_status(self.cfg, self.store, self.state)
            self._send(200, "application/json; charset=utf-8",
                       json.dumps(payload, ensure_ascii=False, indent=1).encode("utf-8"))
        elif path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", _render(self.cfg, self.store, self.state).encode("utf-8"))
        else:
            self._send(404, "application/json", b'{"error":"not found"}')

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):  # pragma: no cover
            pass

    def log_message(self, fmt: str, *args) -> None:
        log.debug("http %s", fmt % args)


class StatusServer:
    def __init__(self, cfg: Config, store: StateStore, state: StatusState | None = None) -> None:
        self.cfg = cfg
        self.store = store
        self.state = state
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def address(self) -> str:
        host, port = self.cfg.server.host_port()
        return "http://%s:%d/" % ("127.0.0.1" if host in ("0.0.0.0", "::") else host, port)

    def start(self) -> None:
        host, port = self.cfg.server.host_port()
        handler = type(
            "BoundHandler",
            (_Handler,),
            {"cfg": self.cfg, "store": self.store, "state": self.state},
        )
        self._server = ThreadingHTTPServer((host, port), handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.5},
                                        daemon=True, name="status-server")
        self._thread.start()
        log.info("status page on %s", self.address)

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None

"""Command line interface.

    bangumi-syncer run            # sync forever (default command inside the container)
    bangumi-syncer run --once     # one shot, useful for cron / debugging
    bangumi-syncer oauth          # obtain a Bangumi token
    bangumi-syncer status         # what happened recently
    bangumi-syncer review         # shows that still need a manual subject
    bangumi-syncer map ...        # inspect / fix season -> subject mappings
    bangumi-syncer search         # look up Bangumi subjects
    bangumi-syncer dump           # what the media server reports, as JSON
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
import unicodedata
from typing import Any

from . import __version__, config, oauth, sources, trackers
from .config import Config, ConfigError
from .log import get_logger, setup_logging
from .models import is_watched
from .settings import SettingsManager
from .sources import SourceError
from .state import StateStore
from .sync import SyncEngine
from .trackers import AuthError, TrackerError
from .ui import UiManager
from .util import format_time, human_duration

log = get_logger("cli")

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_CONFIG = 2
EXIT_AUTH = 3


# --------------------------------------------------------------------- output
def _display_width(text: str) -> int:
    width = 0
    for char in text:
        width += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return width


def _pad(text: str, width: int) -> str:
    text = str(text)
    return text + " " * max(0, width - _display_width(text))


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    if not rows:
        return "  (none)"
    cells = [[str(c) for c in row] for row in rows]
    widths = [_display_width(h) for h in headers]
    for row in cells:
        for index, cell in enumerate(row):
            if index < len(widths):
                widths[index] = max(widths[index], _display_width(cell))
    lines = ["  " + "  ".join(_pad(h, widths[i]) for i, h in enumerate(headers))]
    lines.append("  " + "  ".join("-" * w for w in widths))
    for row in cells:
        lines.append("  " + "  ".join(_pad(cell, widths[i]) for i, cell in enumerate(row)))
    return "\n".join(lines)


def _print_run(cfg: Config, result) -> None:
    print("\n=== sync run %s ===" % ("(dry-run)" if result.dry_run else ""))
    print("  %s" % result.summary())
    print("  duration %s" % human_duration(result.duration_seconds))
    if result.error:
        print("  error: %s" % result.error)
    if result.users:
        print(
            _table(
                ["user", "account", "shows", "matched", "unmatched", "pushed", "skipped", "failed", "error"],
                [
                    [u.user, u.account, u.series_seen, u.matched, u.unmatched, u.pushed, u.skipped, u.failed,
                     u.error or ""]
                    for u in result.users
                ],
            )
        )


# ------------------------------------------------------------------- helpers
def _resolve_account(cfg: Config, args: argparse.Namespace) -> str:
    account = getattr(args, "account", None)
    user = getattr(args, "user", None)
    if account:
        cfg.account(account)  # validates
        return account
    if user:
        for candidate in cfg.users:
            if candidate.fnos_user == user:
                return candidate.account
        raise ConfigError("no configured user matches %r" % user)
    if len(cfg.target.accounts) == 1:
        return cfg.target.accounts[0].name
    raise ConfigError(
        "several accounts configured; pass --account NAME (one of: %s)"
        % ", ".join(a.name for a in cfg.target.accounts)
    )


def _make_tracker(cfg: Config, store: StateStore, account_name: str):
    return trackers.create(cfg.account(account_name), cfg.target, store)


# ------------------------------------------------------------------- commands
def cmd_run(cfg: Config, args: argparse.Namespace) -> int:
    store = StateStore(cfg.app.state_db)
    settings_manager = SettingsManager(cfg, store)
    ui_manager = UiManager(store, os.path.dirname(os.path.abspath(cfg.app.state_db)),
                           user_agent=cfg.target.user_agent)
    source = sources.create(cfg.source)
    engine = SyncEngine(cfg, store, source)

    stop = threading.Event()

    def _handle_signal(signum: int, _frame) -> None:
        log.info("signal %d received, stopping after the current step", signum)
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handle_signal)
        except (ValueError, OSError):  # pragma: no cover - non main thread
            pass

    status_state = None
    status_server = None
    if cfg.server.enabled:
        from .server import StatusServer, StatusState

        status_state = StatusState(started_at=int(time.time()), dry_run=cfg.app.dry_run)
        status_state.source_description = source.describe()
        status_server = StatusServer(cfg, store, status_state, settings=settings_manager, ui=ui_manager)
        try:
            status_server.start()
        except OSError as exc:
            log.error("cannot start the status server on %s: %s", cfg.server.listen, exc)
            status_server = None

    try:
        if args.once:
            result = engine.run()
            if status_state is not None:
                status_state.last_result = result
                status_state.source_description = source.describe()
            _print_run(cfg, result)
            if cfg.app.dry_run:
                print("\n  dry_run=true: nothing was written to Bangumi."
                      " Set app.dry_run = false (or --no-dry-run) to go live.")
            if result.ok:
                return EXIT_OK
            return EXIT_AUTH if result.auth_error else EXIT_FAIL

        interval = int(args.interval or settings_manager.interval_seconds)
        log.info("sync loop started (interval %ds, dry_run=%s)", interval, cfg.app.dry_run)
        while not stop.is_set():
            result = engine.run()
            if status_state is not None:
                status_state.last_result = result
                status_state.source_description = source.describe()
                status_state.next_run_at = int(time.time()) + interval
            if not result.ok and result.error:
                log.warning("run failed: %s", result.error)
                if result.auth_error:
                    log.warning("hint: bangumi-syncer oauth --account <name>")
            # Settings can change while we wait; re-read the interval each cycle.
            interval = int(args.interval or settings_manager.interval_seconds)
            if status_state is not None:
                status_state.next_run_at = int(time.time()) + interval
            # Sleep until the next interval, but wake up early for a manual
            # "sync now" request coming from the web UI.
            deadline = time.time() + interval
            while not stop.is_set() and time.time() < deadline:
                if status_state is not None and status_state.run_requested.is_set():
                    status_state.run_requested.clear()
                    log.info("manual sync request picked up")
                    break
                stop.wait(0.5)
        log.info("sync loop stopped")
    finally:
        if status_server is not None:
            status_server.stop()
        store.close()
    return EXIT_OK


def cmd_oauth(cfg: Config, args: argparse.Namespace) -> int:
    account = cfg.account(args.account)
    store = StateStore(cfg.app.state_db)
    try:
        oauth.login(
            account,
            store,
            cfg.target,
            manual=args.manual,
            open_browser=not args.no_browser,
            timeout=args.timeout,
        )
    finally:
        store.close()
    return EXIT_OK


def cmd_status(cfg: Config, args: argparse.Namespace) -> int:
    from .server import build_status

    store = StateStore(cfg.app.state_db)
    try:
        data = build_status(cfg, store, None)
    finally:
        store.close()
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return EXIT_OK

    tz = cfg.app.timezone
    print("Bangumi-syncer v%s  %s" % (__version__, "DRY-RUN" if cfg.app.dry_run else "LIVE"))
    print("config   %s" % (cfg.path or "-"))
    print("state db %s" % cfg.app.state_db)
    print("source   %s/%s  (%s)" % (
        cfg.source.type,
        cfg.source.mode,
        cfg.source.snapshot_path if cfg.source.mode == "snapshot" else cfg.source.db_path,
    ))
    print("users    %s" % ", ".join("%s -> %s" % (u.fnos_user, u.account) for u in cfg.users))
    stats = data["stats"]
    print("state    mappings=%d (confirmed %d) pushed=%d failed=%d review=%d"
          % (stats["mappings"], stats["mappings_confirmed"], stats["pushed"], stats["failed"], len(data["review"])))
    print("\nAccounts")
    print(_table(
        ["account", "token", "expires", "bangumi user"],
        [[a["name"], "stored" if a.get("has_token") else "MISSING",
          format_time(a.get("expires_at"), tz), a.get("remote_user") or "-"] for a in data["accounts"]],
    ))
    print("\nRecent runs")
    print(_table(
        ["started", "ok", "pushed", "failed", "matched", "unmatched", "message"],
        [[r.get("started_at_human") or "-", "ok" if r.get("ok") else "FAIL", r.get("pushed") or 0,
          r.get("failed") or 0, r.get("matched") or 0, r.get("unmatched") or 0, r.get("message") or ""]
         for r in data["runs"]],
    ))
    if data["review"]:
        print("\n%d show(s) need a manual subject: run `bangumi-syncer review`" % len(data["review"]))
    return EXIT_OK


def cmd_review(cfg: Config, args: argparse.Namespace) -> int:
    store = StateStore(cfg.app.state_db)
    try:
        account = _resolve_account(cfg, args)
        rows = store.candidates(account)
    finally:
        store.close()

    payload = []
    for row in rows:
        payload.append({"account": row["account"], "series_key": row["series_key"], **(row.get("payload") or {})})
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return EXIT_OK

    if not payload:
        print("Nothing to review: every watched show has a mapping (or nothing was played yet).")
        return EXIT_OK
    for item in payload:
        print("\n%s  S%s  (%s, %s eps)" % (
            item.get("title") or item["series_key"],
            item.get("season") or "?",
            item.get("year") or "?",
            item.get("episode_count") or "?",
        ))
        print("  key    %s" % item["series_key"])
        print("  reason %s" % (item.get("reason") or ""))
        candidates = item.get("candidates") or []
        if candidates:
            print(_table(
                ["subject", "score", "name_cn", "name", "year", "eps"],
                [[c["subject_id"], c.get("score"), c.get("name_cn") or "", c.get("name") or "",
                  c.get("year") or "", c.get("eps") or ""] for c in candidates],
            ))
            print("  -> bangumi-syncer map add --account %s --series '%s' --subject %s"
                  % (item["account"], item["series_key"],
                     candidates[0]["subject_id"] if candidates else "<id>"))
        else:
            print("  no search results; find the subject id with `bangumi-syncer search -k \"...\"`")
        print("  -> for a merged season add an explicit range, e.g. --from 25 --to 48")
    return EXIT_OK


def cmd_map(cfg: Config, args: argparse.Namespace) -> int:
    store = StateStore(cfg.app.state_db)
    try:
        account = _resolve_account(cfg, args)
        if args.action == "list":
            rows = store.mappings(account)
            data = [
                {
                    "account": r["account"],
                    "series_key": r["series_key"],
                    "ep_from": r["ep_from"],
                    "ep_to": r["ep_to"],
                    "subject_id": r["subject_id"],
                    "subject_name": r["subject_name"],
                    "strategy": r["strategy"],
                    "confidence": r["confidence"],
                    "confirmed": bool(r["confirmed"]),
                    "pushed": r["pushed"],
                }
                for r in rows
            ]
            if args.json:
                print(json.dumps(data, ensure_ascii=False, indent=2))
            else:
                print(_table(
                    ["series key", "eps", "subject", "name", "strategy", "score", "confirmed", "pushed"],
                    [[r["series_key"], "%s-%s" % (r["ep_from"], r["ep_to"]), r["subject_id"],
                      r["subject_name"] or "", r["strategy"], r["confidence"], "yes" if r["confirmed"] else "no",
                      r["pushed"]] for r in data],
                ))
            return EXIT_OK

        if not args.series:
            raise ConfigError("--series is required (the key shown by `bangumi-syncer review`)")

        if args.action == "add":
            name = args.name
            if not name:
                try:
                    tracker = _make_tracker(cfg, store, account)
                    name = tracker.subject_name(args.subject) or ""
                except (AuthError, TrackerError) as exc:
                    log.debug("could not resolve subject name: %s", exc)
                    name = ""
            store.save_mapping(
                account,
                args.series,
                subject_id=int(args.subject),
                subject_name=name or "",
                strategy="manual",
                confidence=1.0,
                ep_from=int(args.ep_from),
                ep_to=int(args.ep_to),
                confirmed=True,
            )
            removed = store.delete_pushes(account, args.series, int(args.ep_from), int(args.ep_to))
            store.delete_candidates(account, args.series)
            print("mapped %s -> subject %d (%s) for local episodes %s-%s%s"
                  % (args.series, int(args.subject), name or "?", args.ep_from, args.ep_to,
                     "; cleared %d push record(s)" % removed if removed else ""))
            return EXIT_OK

        if args.action == "remove":
            # Resolve the affected range *before* deleting the mapping row.
            target = None
            if args.ep_from is not None:
                for row in store.mapping_groups(account, args.series):
                    if row["ep_from"] == args.ep_from:
                        target = row
                        break
            removed = store.delete_mapping(account, args.series, args.ep_from)
            if target is not None:
                cleared = store.delete_pushes(
                    account, args.series, int(target["ep_from"]), int(target["ep_to"])
                )
            elif args.ep_from is None:
                cleared = store.delete_pushes(account, args.series)
            else:
                cleared = 0
            print("removed %d mapping(s) for %s; cleared %d push record(s)"
                  % (removed, args.series, cleared))
            return EXIT_OK

        raise ConfigError("unknown map action %r" % args.action)
    finally:
        store.close()


def cmd_search(cfg: Config, args: argparse.Namespace) -> int:
    store = StateStore(cfg.app.state_db)
    try:
        account = _resolve_account(cfg, args)
        tracker = _make_tracker(cfg, store, account)
        hits = tracker.search(args.keyword, args.limit, cfg.match.accept_types)
    finally:
        store.close()
    if args.json:
        print(json.dumps([{
            "subject_id": h.subject_id,
            "name": h.name,
            "name_cn": h.name_cn,
            "year": h.year,
            "eps": h.episode_count,
        } for h in hits], ensure_ascii=False, indent=2))
        return EXIT_OK
    print(_table(
        ["subject", "eps", "year", "name", "name_cn"],
        [[h.subject_id, h.episode_count or "", h.year or "", h.name, h.name_cn] for h in hits],
    ))
    return EXIT_OK


def cmd_dump(cfg: Config, args: argparse.Namespace) -> int:
    source = sources.create(cfg.source)
    source.prepare()
    snapshot = source.snapshot()
    by_user = snapshot.by_user()
    series_list = list(snapshot.series)

    if args.json:
        users = []
        for user in snapshot.users:
            plays = by_user.get(user.key, [])
            ratio = cfg.sync.watch_ratio
            users.append({
                "key": user.key,
                "name": user.name,
                "plays": len(plays),
                "finished": sum(
                    1 for p in plays if is_watched(p, ratio, cfg.sync.min_position_seconds)
                ),
            })
        payload = {
            "source": source.describe(),
            "users": users,
            "series": [
                {
                    "key": s.key,
                    "title": s.title,
                    "original_title": s.original_title,
                    "aliases": list(s.aliases),
                    "year": s.year,
                    "season": s.season,
                    "episode_count": s.episode_count,
                    "library_path": s.library_path,
                }
                for s in series_list
            ],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return EXIT_OK

    print("Source: %s" % json.dumps(source.describe(), ensure_ascii=False))
    print("\nUsers:")
    rows = []
    for user in snapshot.users:
        plays = by_user.get(user.key, [])
        finished = sum(1 for p in plays if is_watched(p, cfg.sync.watch_ratio, cfg.sync.min_position_seconds))
        rows.append([user.name, user.key[:8] + "…", len(plays), finished])
    print(_table(["user", "guid", "plays", "finished (ratio %.2f)" % cfg.sync.watch_ratio], rows))
    print("\nRecognised anime seasons:")
    print(_table(
        ["title", "S", "eps", "year", "key"],
        [[s.title, s.season, s.episode_count, s.year or "", s.key] for s in series_list],
    ))
    return EXIT_OK


# --------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bangumi-syncer",
        description="Sync fnOS (飞牛影视) anime watch progress to Bangumi.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Docs: https://github.com/wwb051221/Bangumi-syncer",
    )
    parser.add_argument("--config", help="path to config.toml (default: /config/config.toml or ./config.toml)")
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], help="override app.log_level")
    parser.add_argument("--log-format", choices=["text", "json"], help="override app.log_format")
    parser.add_argument("--dry-run", dest="dry_run", action="store_true", default=None,
                        help="never write to Bangumi (default from config)")
    parser.add_argument("--no-dry-run", dest="dry_run", action="store_false",
                        help="really write to Bangumi")
    parser.add_argument("--version", action="version", version="bangumi-syncer " + __version__)

    subparsers = parser.add_subparsers(dest="command", metavar="command")
    subparsers.required = True

    run = subparsers.add_parser("run", help="run the sync (loop by default)")
    run.add_argument("--once", action="store_true", help="run once and exit")
    run.add_argument("--interval", type=int, help="seconds between runs (default from sync.interval_seconds)")
    run.set_defaults(func=cmd_run)

    oauth_cmd = subparsers.add_parser("oauth", help="authorise a Bangumi account")
    oauth_cmd.add_argument("--account", required=True)
    oauth_cmd.add_argument("--manual", action="store_true", help="print the URL and paste the code manually")
    oauth_cmd.add_argument("--no-browser", action="store_true", help="do not try to open a browser")
    oauth_cmd.add_argument("--timeout", type=int, default=300, help="seconds to wait for the callback")
    oauth_cmd.set_defaults(func=cmd_oauth)

    status = subparsers.add_parser("status", help="show recent runs, mappings and token state")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=cmd_status)

    review = subparsers.add_parser("review", help="list shows that still need a manual subject")
    review.add_argument("--account")
    review.add_argument("--user", help="fnOS user (resolves its account)")
    review.add_argument("--json", action="store_true")
    review.set_defaults(func=cmd_review)

    mapping = subparsers.add_parser("map", help="inspect or fix season -> subject mappings")
    map_sub = mapping.add_subparsers(dest="action", metavar="action")
    map_sub.required = True

    def _scope(parser: argparse.ArgumentParser) -> None:
        """--account/--user are accepted after the action, e.g. `map add --account main`."""
        parser.add_argument("--account")
        parser.add_argument("--user")

    map_list = map_sub.add_parser("list", help="list mappings")
    map_list.add_argument("--json", action="store_true")
    _scope(map_list)
    map_list.set_defaults(func=cmd_map)

    map_add = map_sub.add_parser("add", help="add or override a mapping")
    map_add.add_argument("--series", required=True, help="series key from `bangumi-syncer review`")
    map_add.add_argument("--subject", required=True, type=int, help="Bangumi subject id")
    map_add.add_argument("--from", dest="ep_from", type=int, default=1, help="first local episode (default 1)")
    map_add.add_argument("--to", dest="ep_to", type=int, default=9999, help="last local episode (default 9999)")
    map_add.add_argument("--name", help="subject name for display (fetched when omitted)")
    _scope(map_add)
    map_add.set_defaults(func=cmd_map)

    map_remove = map_sub.add_parser("remove", help="remove a mapping")
    map_remove.add_argument("--series", required=True)
    map_remove.add_argument("--from", dest="ep_from", type=int, help="only this range start")
    _scope(map_remove)
    map_remove.set_defaults(func=cmd_map)

    search = subparsers.add_parser("search", help="search Bangumi subjects by keyword")
    search.add_argument("--keyword", "-k", required=True)
    search.add_argument("--limit", type=int, default=8)
    search.add_argument("--account")
    search.add_argument("--user")
    search.add_argument("--json", action="store_true")
    search.set_defaults(func=cmd_search)

    dump = subparsers.add_parser("dump", help="print what the media server reports (no writes)")
    dump.add_argument("--json", action="store_true")
    dump.set_defaults(func=cmd_dump)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        cfg = config.load(args.config)
    except ConfigError as exc:
        print("config error: %s" % exc, file=sys.stderr)
        return EXIT_CONFIG

    if args.log_level:
        cfg.app.log_level = args.log_level
    if args.log_format:
        cfg.app.log_format = args.log_format
    if args.dry_run is not None:
        cfg.app.dry_run = args.dry_run
    setup_logging(cfg.app.log_level, cfg.app.log_format)

    try:
        return int(args.func(cfg, args))
    except ConfigError as exc:
        print("config error: %s" % exc, file=sys.stderr)
        return EXIT_CONFIG
    except AuthError as exc:
        print("auth error: %s" % exc, file=sys.stderr)
        return EXIT_AUTH
    except (SourceError, TrackerError, oauth.OAuthError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return EXIT_FAIL
    except KeyboardInterrupt:  # pragma: no cover
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

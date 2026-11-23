"""Persistent state: OAuth tokens, season→subject mappings, push log, run log.

Keeping this in SQLite means restarts are cheap and the sync stays idempotent:
the same episode is never pushed twice unless its state actually changed.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from .log import get_logger

log = get_logger("state")

SCHEMA_VERSION = 1

SCHEMA = """
create table if not exists meta (
    key   text primary key,
    value text not null
);

create table if not exists accounts (
    name          text primary key,
    provider      text not null default 'bangumi',
    access_token  text,
    refresh_token text,
    expires_at    integer not null default 0,
    remote_user   text,
    updated_at    integer not null default 0
);

create table if not exists mappings (
    account      text    not null,
    series_key   text    not null,
    ep_from      integer not null default 1,
    ep_to        integer not null default 9999,
    subject_id   integer not null,
    subject_name text,
    strategy     text    not null default 'manual',
    confidence   real    not null default 0,
    confirmed    integer not null default 0,
    created_at   integer not null default 0,
    updated_at   integer not null default 0,
    primary key (account, series_key, ep_from, ep_to)
);
create index if not exists mappings_lookup on mappings (account, series_key);

create table if not exists pushes (
    account      text    not null,
    series_key   text    not null,
    episode      integer not null,
    subject_id   integer,
    episode_id   integer,
    state        text    not null,
    position_seconds integer not null default 0,
    watched      integer not null default 0,
    attempts     integer not null default 0,
    error        text,
    updated_at   integer not null default 0,
    primary key (account, series_key, episode)
);

create table if not exists candidates (
    account    text not null,
    series_key text not null,
    payload    text not null,
    updated_at integer not null default 0,
    primary key (account, series_key)
);

create table if not exists runs (
    id          integer primary key autoincrement,
    started_at  integer not null,
    finished_at integer,
    ok          integer,
    users       integer not null default 0,
    pushed      integer not null default 0,
    failed      integer not null default 0,
    matched     integer not null default 0,
    unmatched   integer not null default 0,
    message     text
);
"""


@dataclass(slots=True)
class Tokens:
    access_token: str
    refresh_token: str
    expires_at: int  # epoch seconds
    remote_user: str | None = None

    def needs_refresh(self, margin: int = 90) -> bool:
        return not self.expires_at or self.expires_at - margin <= int(time.time())


class StateStore:
    """Thread safe wrapper around the state database."""

    def __init__(self, path: str) -> None:
        import os

        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock, self._conn:
            self._conn.execute("pragma journal_mode=wal")
            self._conn.execute("pragma synchronous=normal")
            self._conn.execute("pragma foreign_keys=on")
            self._conn.executescript(SCHEMA)
            self._conn.execute(
                "insert into meta(key, value) values('schema_version', ?) "
                "on conflict(key) do update set value=excluded.value",
                (str(SCHEMA_VERSION),),
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ misc
    def meta(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self._conn.execute("select value from meta where key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "insert into meta(key, value) values(?, ?) "
                "on conflict(key) do update set value=excluded.value",
                (key, value),
            )

    def get_json(self, key: str) -> Any:
        raw = self.meta(key)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return None

    def set_json(self, key: str, value: Any) -> None:
        self.set_meta(key, json.dumps(value, ensure_ascii=False))

    def delete_meta(self, key: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("delete from meta where key=?", (key,))

    # -------------------------------------------------------------- accounts
    def tokens(self, account: str) -> Tokens | None:
        with self._lock:
            row = self._conn.execute(
                "select access_token, refresh_token, expires_at, remote_user from accounts where name=?",
                (account,),
            ).fetchone()
        if not row or not row["access_token"]:
            return None
        return Tokens(
            access_token=row["access_token"],
            refresh_token=row["refresh_token"] or "",
            expires_at=int(row["expires_at"] or 0),
            remote_user=row["remote_user"],
        )

    def save_tokens(self, account: str, tokens: Tokens, provider: str = "bangumi") -> None:
        now = int(time.time())
        with self._lock, self._conn:
            self._conn.execute(
                "insert into accounts(name, provider, access_token, refresh_token, expires_at, remote_user, updated_at) "
                "values(?,?,?,?,?,?,?) on conflict(name) do update set "
                "provider=excluded.provider, access_token=excluded.access_token, "
                "refresh_token=excluded.refresh_token, expires_at=excluded.expires_at, "
                "remote_user=coalesce(excluded.remote_user, accounts.remote_user), "
                "updated_at=excluded.updated_at",
                (account, provider, tokens.access_token, tokens.refresh_token, tokens.expires_at, tokens.remote_user, now),
            )

    def clear_tokens(self, account: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "update accounts set access_token=null, refresh_token=null, expires_at=0, updated_at=? where name=?",
                (int(time.time()), account),
            )

    def accounts(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "select name, provider, expires_at, remote_user, updated_at, "
                "       case when access_token is null then 0 else 1 end as has_token "
                "from accounts order by name"
            ).fetchall()
        return [dict(r) for r in rows]

    # -------------------------------------------------------------- mappings
    def mapping_for(self, account: str, series_key: str, episode: int) -> sqlite3.Row | None:
        """Narrowest mapping range that covers ``episode`` (manual wins by range width)."""
        with self._lock:
            return self._conn.execute(
                "select * from mappings where account=? and series_key=? and ep_from<=? and ep_to>=? "
                "order by (ep_to - ep_from) asc, confirmed desc, updated_at desc limit 1",
                (account, series_key, episode, episode),
            ).fetchone()

    def mapping_groups(self, account: str, series_key: str) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "select * from mappings where account=? and series_key=? order by ep_from",
                (account, series_key),
            ).fetchall()

    def mappings(self, account: str | None = None) -> list[sqlite3.Row]:
        query = (
            "select m.*, (select count(*) from pushes p where p.account=m.account "
            "            and p.series_key=m.series_key and p.episode between m.ep_from and m.ep_to "
            "            and p.state='sent') as pushed "
            "from mappings m "
        )
        params: tuple[Any, ...] = ()
        if account:
            query += "where m.account=? "
            params = (account,)
        query += "order by m.account, m.series_key, m.ep_from"
        with self._lock:
            return self._conn.execute(query, params).fetchall()

    def save_mapping(
        self,
        account: str,
        series_key: str,
        subject_id: int,
        subject_name: str,
        strategy: str,
        confidence: float = 0.0,
        ep_from: int = 1,
        ep_to: int = 9999,
        confirmed: bool = False,
    ) -> None:
        now = int(time.time())
        with self._lock, self._conn:
            self._conn.execute(
                "insert into mappings(account, series_key, ep_from, ep_to, subject_id, subject_name, "
                "strategy, confidence, confirmed, created_at, updated_at) values(?,?,?,?,?,?,?,?,?,?,?) "
                "on conflict(account, series_key, ep_from, ep_to) do update set "
                "subject_id=excluded.subject_id, subject_name=excluded.subject_name, "
                "strategy=excluded.strategy, confidence=excluded.confidence, "
                "confirmed=excluded.confirmed, updated_at=excluded.updated_at",
                (account, series_key, ep_from, ep_to, subject_id, subject_name, strategy, confidence,
                 1 if confirmed else 0, now, now),
            )

    def delete_mapping(self, account: str, series_key: str, ep_from: int | None = None) -> int:
        with self._lock, self._conn:
            if ep_from is None:
                cur = self._conn.execute(
                    "delete from mappings where account=? and series_key=?", (account, series_key)
                )
            else:
                cur = self._conn.execute(
                    "delete from mappings where account=? and series_key=? and ep_from=?",
                    (account, series_key, ep_from),
                )
            return cur.rowcount

    # ----------------------------------------------------------------- pushes
    def push(self, account: str, series_key: str, episode: int) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "select * from pushes where account=? and series_key=? and episode=?",
                (account, series_key, episode),
            ).fetchone()

    def record_push(
        self,
        account: str,
        series_key: str,
        episode: int,
        state: str,
        subject_id: int | None = None,
        episode_id: int | None = None,
        position_seconds: int = 0,
        watched: bool = False,
        error: str | None = None,
    ) -> None:
        now = int(time.time())
        with self._lock, self._conn:
            self._conn.execute(
                "insert into pushes(account, series_key, episode, subject_id, episode_id, state, "
                "position_seconds, watched, error, updated_at) values(?,?,?,?,?,?,?,?,?,?) "
                "on conflict(account, series_key, episode) do update set "
                "subject_id=excluded.subject_id, episode_id=excluded.episode_id, state=excluded.state, "
                "position_seconds=excluded.position_seconds, watched=excluded.watched, "
                "attempts=pushes.attempts + 1, error=excluded.error, updated_at=excluded.updated_at",
                (account, series_key, episode, subject_id, episode_id, state,
                 0 if state == "failed" else position_seconds, 1 if watched else 0, error, now),
            )

    def pushes(self, account: str | None = None, limit: int = 100, state: str | None = None) -> list[sqlite3.Row]:
        query = "select * from pushes where 1=1 "
        params: list[Any] = []
        if account:
            query += "and account=? "
            params.append(account)
        if state:
            query += "and state=? "
            params.append(state)
        query += "order by updated_at desc limit ?"
        params.append(limit)
        with self._lock:
            return self._conn.execute(query, params).fetchall()

    def delete_pushes(self, account: str, series_key: str, ep_from: int | None = None,
                      ep_to: int | None = None) -> int:
        """Forget push history, e.g. after a mapping was corrected."""
        with self._lock, self._conn:
            if ep_from is None:
                cur = self._conn.execute(
                    "delete from pushes where account=? and series_key=?", (account, series_key)
                )
            else:
                cur = self._conn.execute(
                    "delete from pushes where account=? and series_key=? and episode between ? and ?",
                    (account, series_key, ep_from, ep_to if ep_to is not None else 99999),
                )
            return cur.rowcount

    # ------------------------------------------------------------- candidates
    def save_candidates(self, account: str, series_key: str, payload: dict[str, Any]) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "insert into candidates(account, series_key, payload, updated_at) values(?,?,?,?) "
                "on conflict(account, series_key) do update set payload=excluded.payload, "
                "updated_at=excluded.updated_at",
                (account, series_key, json.dumps(payload, ensure_ascii=False), int(time.time())),
            )

    def candidates(self, account: str | None = None) -> list[dict[str, Any]]:
        query = "select * from candidates "
        params: tuple[Any, ...] = ()
        if account:
            query += "where account=? "
            params = (account,)
        query += "order by updated_at desc"
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            try:
                item["payload"] = json.loads(item.pop("payload"))
            except (ValueError, TypeError):
                item["payload"] = {}
            out.append(item)
        return out

    def delete_candidates(self, account: str, series_key: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("delete from candidates where account=? and series_key=?", (account, series_key))

    # ------------------------------------------------------------------- runs
    def start_run(self) -> int:
        with self._lock, self._conn:
            cur = self._conn.execute("insert into runs(started_at) values(?)", (int(time.time()),))
            return int(cur.lastrowid)

    def finish_run(
        self,
        run_id: int,
        ok: bool,
        users: int = 0,
        pushed: int = 0,
        failed: int = 0,
        matched: int = 0,
        unmatched: int = 0,
        message: str | None = None,
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "update runs set finished_at=?, ok=?, users=?, pushed=?, failed=?, matched=?, unmatched=?, message=? "
                "where id=?",
                (int(time.time()), 1 if ok else 0, users, pushed, failed, matched, unmatched, message, run_id),
            )

    def runs(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("select * from runs order by id desc limit ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def last_run(self) -> dict[str, Any] | None:
        rows = self.runs(1)
        return rows[0] if rows else None

    def stats(self) -> dict[str, int]:
        with self._lock:
            def count(sql: str, params: Iterable[Any] = ()) -> int:
                row = self._conn.execute(sql, tuple(params)).fetchone()
                return int(row[0] or 0)

            return {
                "mappings": count("select count(*) from mappings"),
                "mappings_confirmed": count("select count(*) from mappings where confirmed=1"),
                "candidates": count("select count(*) from candidates"),
                "pushed": count("select count(*) from pushes where state='sent'"),
                "failed": count("select count(*) from pushes where state='failed'"),
            }

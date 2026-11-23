"""fnOS 飞牛影视 adapter.

The fnOS media app (``trim.media``) keeps everything in one SQLite database at
``/usr/local/apps/@appdata/trim.media/database/trimmedia.db`` which is
``root:root 0700`` with journal mode ``delete`` (verified on fnOS 1.2.1102).

Design notes
------------
* We never write to the server database. In ``live`` mode we ask SQLite for a
  consistent copy with ``VACUUM INTO`` over a read-only connection, which is
  safe while the app keeps writing (it does not block the server either).
  In ``snapshot`` mode we simply read a copy someone else prepared.
* Schema drift is expected (the app ships with the OS and is closed source), so
  the adapter probes for the columns it needs and raises a clear error listing
  what is missing. Two query overrides in ``[source.fnos]`` allow adapting to a
  new layout without touching Python code.
* Only episodes are handled; movies and live channels are ignored.
"""

from __future__ import annotations

import json
import os
import sqlite3
import urllib.parse
from typing import Any

from ..config import SourceConfig
from ..log import get_logger
from ..models import Episode, PlayState, SeriesRef, SourceSnapshot, SourceUser
from .base import SourceAdapter, SourceError

log = get_logger("source.fnos")

REQUIRED_COLUMNS: dict[str, tuple[str, ...]] = {
    "item": ("guid", "type", "title", "parent_guid", "season_number", "episode_number", "runtime"),
    "item_user_play": ("item_guid", "user_guid", "ts", "watched", "update_time"),
    "item_media": ("item_guid", "path"),
    "user": ("guid", "username", "status"),
}

# Every identified episode of every season. ``ep_guid`` is the join key with the plays query.
DEFAULT_EPISODES_QUERY = """
select tv.guid                        as tv_guid,
       tv.title                       as tv_title,
       tv.original_title              as tv_original_title,
       tv.first_air_date              as tv_first_air_date,
       tv.tmdb_id                     as tv_tmdb_id,
       tv.alternative_titles          as tv_alt_titles,
       coalesce(s.season_number, 0)   as season_number,
       ep.guid                        as ep_guid,
       ep.episode_number              as episode_number,
       ep.title                       as ep_title,
       ep.runtime                     as ep_runtime,
       (select m.path from item_media m where m.item_guid = ep.guid order by m.sort_num limit 1) as media_path
from item ep
join item s  on s.guid  = ep.parent_guid and s.type = 'Season'
join item tv on tv.guid = s.parent_guid  and tv.type = 'TV'
where ep.type = 'Episode'
  and ep.episode_number is not null
  and ep.episode_number > 0
order by tv.title, season_number, ep.episode_number
"""

# Newest play record per (user, episode).
DEFAULT_PLAYS_QUERY = """
select p.user_guid    as user_guid,
       u.username     as username,
       p.item_guid    as ep_guid,
       p.ts           as ts,
       p.watched      as watched,
       p.update_time  as update_time
from item_user_play p
join item i   on i.guid = p.item_guid and i.type = 'Episode'
join "user" u on u.guid = p.user_guid
where p.update_time = (
        select max(p2.update_time)
        from item_user_play p2
        where p2.item_guid = p.item_guid and p2.user_guid = p.user_guid)
"""

# All real accounts, so "no anime played yet" is not reported as "unknown user".
DEFAULT_USERS_QUERY = """
select guid as user_guid, username
from "user"
where coalesce(status, 1) = 1
  and guid <> 'default-user-template'
"""


def _connect_readonly(path: str, immutable: bool = False) -> sqlite3.Connection:
    quoted = urllib.parse.quote(os.path.abspath(path).replace("\\", "/"), safe="/:")
    params = "mode=ro&immutable=1" if immutable else "mode=ro"
    return sqlite3.connect("file:%s?%s" % (quoted, params), uri=True, timeout=30)


def _year(value: Any) -> int | None:
    if not value:
        return None
    text = str(value)
    if len(text) >= 4 and text[:4].isdigit():
        return int(text[:4])
    return None


def _aliases(raw: Any) -> tuple[str, ...]:
    """fnOS stores TMDB alternative titles as ``{"titles":[{"title": "..."}]}``."""
    if not raw:
        return ()
    try:
        data = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
    except (ValueError, TypeError):
        return ()
    titles: list[str] = []
    if isinstance(data, dict):
        for item in data.get("titles") or []:
            if isinstance(item, dict) and item.get("title"):
                titles.append(str(item["title"]).strip())
            elif isinstance(item, str):
                titles.append(item.strip())
    elif isinstance(data, list):
        titles.extend(str(i).strip() for i in data if isinstance(i, str))
    return tuple(t for t in titles if t)


def path_allowed(path: str | None, includes: list[str], excludes: list[str]) -> bool:
    """Directory-prefix filter used to keep only the anime library (and friends)."""
    if not path:
        return not includes
    normalised = path.replace("\\", "/")
    if not normalised.startswith("/"):
        normalised = "/" + normalised

    def under(prefix: str) -> bool:
        return normalised == prefix or normalised.startswith(prefix.rstrip("/") + "/")

    if includes and not any(under(p) for p in includes):
        return False
    if excludes and any(under(p) for p in excludes):
        return False
    return True


class FnosSource(SourceAdapter):
    name = "fnos"

    def __init__(self, cfg: SourceConfig) -> None:
        super().__init__(cfg)
        self._db_path = ""
        self._stats: dict[str, Any] = {}

    # --------------------------------------------------------------- prepare
    def prepare(self) -> None:
        if self.cfg.mode == "snapshot":
            if not os.path.isfile(self.cfg.snapshot_path):
                raise SourceError(
                    "snapshot file not found: %s (set source.snapshot_path or switch source.mode to 'live')"
                    % self.cfg.snapshot_path
                )
            self._db_path = self.cfg.snapshot_path
        else:
            self._db_path = self._refresh_snapshot()
        self._check_schema()

    def _refresh_snapshot(self) -> str:
        src = self.cfg.db_path
        if not os.path.isfile(src):
            raise SourceError(
                "fnOS database not found: %s. Mount the trim.media database directory read-only, e.g. "
                "-v /usr/local/apps/@appdata/trim.media/database:/source:ro" % src
            )
        dst = os.path.abspath(self.cfg.snapshot_path)
        os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
        tmp = dst + ".tmp"
        for leftover in (tmp, tmp + "-journal", tmp + "-wal", tmp + "-shm"):
            try:
                os.remove(leftover)
            except OSError:
                pass

        errors: list[str] = []
        for immutable in (False, True):
            conn: sqlite3.Connection | None = None
            try:
                conn = _connect_readonly(src, immutable=immutable)
                conn.execute("VACUUM INTO ?", (tmp,))
                conn.close()
                conn = None
                os.replace(tmp, dst)
                break
            except sqlite3.Error as exc:  # pragma: no cover - depends on server state
                errors.append("%s%s" % ("immutable: " if immutable else "", exc))
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        else:  # pragma: no cover - both strategies failed
            raise SourceError(
                "could not snapshot %s read-only (%s). Check the bind mount and that the fnOS app is running."
                % (src, "; ".join(errors))
            )

        try:
            chk = _connect_readonly(dst)
            result = chk.execute("pragma quick_check").fetchone()
            chk.close()
            if not result or str(result[0]).lower() != "ok":
                raise SourceError("snapshot failed integrity check: %r" % (result,))
        except sqlite3.Error as exc:
            raise SourceError("snapshot cannot be opened: %s" % exc) from exc
        self._stats["snapshot_path"] = dst
        self._stats["snapshot_size"] = os.path.getsize(dst)
        log.debug("snapshot refreshed: %s (%d bytes)", dst, self._stats["snapshot_size"])
        return dst

    def _check_schema(self) -> None:
        if self.cfg.fnos.episodes_query and self.cfg.fnos.plays_query and self.cfg.fnos.users_query:
            log.debug("custom queries configured, skipping schema probe")
            return
        try:
            conn = _connect_readonly(self._db_path)
        except sqlite3.Error as exc:
            raise SourceError("cannot open %s: %s" % (self._db_path, exc)) from exc
        try:
            tables = {
                row[0] for row in conn.execute("select name from sqlite_master where type='table'").fetchall()
            }
            missing: list[str] = []
            for table, columns in REQUIRED_COLUMNS.items():
                if table not in tables:
                    missing.append("table %s" % table)
                    continue
                present = {row[1] for row in conn.execute('pragma table_info("%s")' % table).fetchall()}
                for column in columns:
                    if column not in present:
                        missing.append("%s.%s" % (table, column))
            if missing:
                raise SourceError(
                    "fnOS database schema is not what this version expects; missing: %s. "
                    "If the app was updated, override [source.fnos] episodes_query/plays_query - "
                    "see docs/schema.md." % ", ".join(missing)
                )
        finally:
            conn.close()

    # -------------------------------------------------------------- snapshot
    def snapshot(self) -> SourceSnapshot:
        try:
            conn = _connect_readonly(self._db_path)
        except sqlite3.Error as exc:
            raise SourceError("cannot open %s: %s" % (self._db_path, exc)) from exc
        conn.row_factory = sqlite3.Row
        try:
            episode_rows = conn.execute(self.cfg.fnos.episodes_query or DEFAULT_EPISODES_QUERY).fetchall()
            play_rows = conn.execute(self.cfg.fnos.plays_query or DEFAULT_PLAYS_QUERY).fetchall()
            user_rows = conn.execute(self.cfg.fnos.users_query or DEFAULT_USERS_QUERY).fetchall()
        except sqlite3.Error as exc:
            raise SourceError(
                "querying the fnOS database failed: %s. If the schema changed, override "
                "[source.fnos] episodes_query/plays_query." % exc
            ) from exc
        finally:
            conn.close()

        # ---- group episodes per (show, season), applying the library filter
        # season -> {episode_number: (row, path)} keeps one row per episode number
        seasons: dict[tuple[str, int], dict[int, tuple[sqlite3.Row, str | None]]] = {}
        skipped_path = 0
        for row in episode_rows:
            season = int(row["season_number"] or 0)
            if self.cfg.skip_season_zero and season <= 0:
                continue
            number = int(row["episode_number"] or 0)
            if number <= 0:
                continue
            path = row["media_path"]
            if not path_allowed(path, self.cfg.include_paths, self.cfg.exclude_paths):
                skipped_path += 1
                continue
            seasons.setdefault((row["tv_guid"], season), {})[number] = (row, path)

        episodes: dict[str, Episode] = {}
        series_by_key: dict[str, SeriesRef] = {}
        for (tv_guid, season), numbers in seasons.items():
            if len(numbers) < max(1, self.cfg.min_episodes):
                continue
            first_row, first_path = next(iter(numbers.values()))
            series = SeriesRef(
                key=self._series_key(tv_guid, season),
                title=str(first_row["tv_title"] or "").strip(),
                original_title=(first_row["tv_original_title"] or None),
                aliases=_aliases(first_row["tv_alt_titles"]),
                year=_year(first_row["tv_first_air_date"]),
                season=season,
                episode_count=len(numbers),
                library_path=first_path,
            )
            series_by_key[series.key] = series
            for number, (row, _path) in numbers.items():
                runtime_minutes = row["ep_runtime"] or 0
                episodes[row["ep_guid"]] = Episode(
                    series=series,
                    number=number,
                    title=(row["ep_title"] or None),
                    runtime_seconds=int(runtime_minutes) * 60 if runtime_minutes else None,
                )

        # ---- attach newest play state per user
        users: dict[str, SourceUser] = {}
        for row in user_rows:
            key = row["user_guid"]
            users[key] = SourceUser(key=key, name=str(row["username"] or key))
        plays: list[PlayState] = []
        for row in play_rows:
            episode = episodes.get(row["ep_guid"])
            if episode is None:
                continue
            user_key = row["user_guid"]
            users.setdefault(user_key, SourceUser(key=user_key, name=str(row["username"] or user_key)))
            update_ms = int(row["update_time"] or 0)
            plays.append(
                PlayState(
                    user_key=user_key,
                    episode=episode,
                    position_seconds=int(row["ts"] or 0),
                    finished_flag=bool(int(row["watched"] or 0)),
                    last_played_at=update_ms // 1000 if update_ms else 0,
                )
            )

        self._stats.update(
            {
                "episode_rows": len(episode_rows),
                "play_rows": len(play_rows),
                "seasons": len({hop.series.key for hop in episodes.values()}),
                "episodes": len(episodes),
                "plays_matched": len(plays),
                "episodes_filtered_by_path": skipped_path,
                "users": len(users),
            }
        )
        log.debug("source stats: %s", self._stats)
        return SourceSnapshot(
            users=list(users.values()),
            plays=plays,
            series=sorted(series_by_key.values(), key=lambda s: (s.title, s.season)),
        )

    # ----------------------------------------------------------------- utils
    @staticmethod
    def _series_key(tv_guid: str, season: int) -> str:
        return "fnos:%s:s%d" % (tv_guid, season)

    def describe(self) -> dict[str, Any]:
        return {
            "type": self.name,
            "mode": self.cfg.mode,
            "db_path": self.cfg.db_path,
            "snapshot_path": self.cfg.snapshot_path,
            "include_paths": self.cfg.include_paths,
            "exclude_paths": self.cfg.exclude_paths,
            "stats": dict(self._stats),
        }

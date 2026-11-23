"""Shared fixtures: a synthetic fnOS-style database, configs and a fake tracker."""

from __future__ import annotations

import sqlite3

import pytest

from fnos2bgm.config import (
    AccountConfig,
    AppConfig,
    Config,
    FnosOptions,
    MatchConfig,
    ServerConfig,
    SourceConfig,
    SyncConfig,
    TargetConfig,
    UserConfig,
)
from fnos2bgm.trackers import AuthError, SubjectHit, TrackEpisode, Tracker
from fnos2bgm.trackers.base import EP_COLLECTED

ANIME_DIR = "/vol1/1000/\u5f71\u7247/\u52a8\u6f2b"  # 影片/动漫
DRAMA_DIR = "/vol1/1000/\u5f71\u7247/\u7535\u89c6\u5267"  # 影片/电视剧

SCHEMA = """
create table "user" (
    guid text primary key,
    username text,
    status integer
);
create table item (
    guid text primary key,
    type text,
    title text,
    original_title text,
    sort_title text,
    first_air_date text,
    last_air_date text,
    air_date text,
    tmdb_id integer,
    alternative_titles text,
    parent_guid text,
    season_number integer,
    episode_number integer,
    runtime integer,
    number_of_seasons integer,
    number_of_episodes integer,
    external_ids text
);
create table item_media (
    guid text primary key,
    item_guid text not null,
    path text not null,
    sort_num integer default 0
);
create table item_user_play (
    item_guid text not null,
    user_guid text not null,
    ts integer default 0,
    watched integer default 0,
    update_time integer,
    type text
);
"""


def _insert(db: sqlite3.Connection, table: str, rows: list[dict]) -> None:
    for row in rows:
        columns = ", ".join(row)
        placeholders = ", ".join("?" for _ in row)
        db.execute(
            'insert into "%s" (%s) values (%s)' % (table, columns, placeholders),
            tuple(row.values()),
        )


def build_fnos_db(path: str) -> str:
    """Create a database that mirrors the real trim.media layout closely enough."""
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)

    _insert(db, "user", [
        {"guid": "u1", "username": "alice", "status": 1},
        {"guid": "u2", "username": "bob", "status": 1},
        {"guid": "default-user-template", "username": "user-template", "status": None},
    ])

    # --- anime #1: a normal single-season show (24 eps, 24 min each)
    _insert(db, "item", [
        {"guid": "tv1", "type": "TV", "title": "\u836f\u5c4b\u5c11\u5973\u7684\u5462\u5583",
         "original_title": "\u85ac\u5c4b\u306e\u3072\u3068\u308a\u3054\u3068",
         "first_air_date": "2023-10-22", "tmdb_id": 220542,
         "alternative_titles": '{"titles":[{"title":"\u85ac\u5c4b\u306e\u3072\u3068\u308a\u3054\u3068"}]}'},
        {"guid": "season1", "type": "Season", "title": "\u7b2c 1 \u5b63",
         "parent_guid": "tv1", "season_number": 1},
        {"guid": "season0", "type": "Season", "title": "\u7279\u522b\u7bc7",
         "parent_guid": "tv1", "season_number": 0},
    ])
    for number in range(1, 25):
        _insert(db, "item", [{
            "guid": "ep%d" % number, "type": "Episode",
            "title": "\u7b2c %d \u96c6" % number, "parent_guid": "season1",
            "season_number": 1, "episode_number": number, "runtime": 24,
        }])
        _insert(db, "item_media", [{
            "guid": "m%d" % number, "item_guid": "ep%d" % number,
            "path": "%s/\u836f\u5c4b\u5c11\u5973\u7684\u5462\u5583 (2023)/S01E%02d.mp4" % (ANIME_DIR, number),
        }])
    _insert(db, "item", [{
        "guid": "spec1", "type": "Episode", "title": "\u7279\u522b\u7bc7",
        "parent_guid": "season0", "season_number": 0, "episode_number": 1, "runtime": 24,
    }])
    _insert(db, "item_media", [{
        "guid": "mspec1", "item_guid": "spec1",
        "path": "%s/\u836f\u5c4b\u5c11\u5973\u7684\u5462\u5583 (2023)/Specials/S00E01.mp4" % ANIME_DIR,
    }])

    # --- anime #2: two seasons merged into one Season 1 (52 episodes)
    _insert(db, "item", [
        {"guid": "tv2", "type": "TV", "title": "\u8fdb\u51fb\u7684\u5de8\u4eba",
         "original_title": "\u9032\u6483\u306e\u5de8\u4eba",
         "first_air_date": "2013-04-07", "tmdb_id": 1429},
        {"guid": "season2", "type": "Season", "title": "Season 1",
         "parent_guid": "tv2", "season_number": 1},
    ])
    for number in range(1, 53):
        _insert(db, "item", [{
            "guid": "aot%d" % number, "type": "Episode",
            "parent_guid": "season2", "season_number": 1,
            "episode_number": number, "runtime": 24,
        }])
        _insert(db, "item_media", [{
            "guid": "maot%d" % number, "item_guid": "aot%d" % number,
            "path": "%s/\u8fdb\u51fb\u7684\u5de8\u4eba/S01E%02d.mkv" % (ANIME_DIR, number),
        }])

    # --- a drama that must be filtered out by include_paths
    _insert(db, "item", [
        {"guid": "tv3", "type": "TV", "title": "\u5927\u660e\u738b\u671d1566",
         "original_title": "\u5927\u660e\u738b\u671d1566", "first_air_date": "2007-01-08"},
        {"guid": "season3", "type": "Season", "title": "\u7b2c 1 \u5b63",
         "parent_guid": "tv3", "season_number": 1},
    ])
    for number in range(1, 4):
        _insert(db, "item", [{
            "guid": "drm%d" % number, "type": "Episode", "parent_guid": "season3",
            "season_number": 1, "episode_number": number, "runtime": 46,
        }])
        _insert(db, "item_media", [{
            "guid": "mdrm%d" % number, "item_guid": "drm%d" % number,
            "path": "%s/\u5927\u660e\u738b\u671d1566/S01E%02d.mp4" % (DRAMA_DIR, number),
        }])

    # --- live channels and movies write play records too; they must never leak in
    _insert(db, "item", [{"guid": "live1", "type": "LiveChannel", "title": "CCTV-1"}])
    _insert(db, "item", [{"guid": "movie1", "type": "Movie", "title": "\u6211\u7684\u540d\u5b57",
                          "runtime": 106}])

    _insert(db, "item_user_play", [
        # alice finished ep1, and stopped at 1300s of ep2 (24min = 1440s -> 90.3%)
        {"item_guid": "ep1", "user_guid": "u1", "ts": 0, "watched": 1,
         "update_time": 1_700_000_000_000, "type": "Episode"},
        # an older record for ep2 must be ignored in favour of the newest one
        {"item_guid": "ep2", "user_guid": "u1", "ts": 60, "watched": 0,
         "update_time": 1_600_000_000_000, "type": "Episode"},
        {"item_guid": "ep2", "user_guid": "u1", "ts": 1300, "watched": 0,
         "update_time": 1_700_000_100_000, "type": "Episode"},
        # alice also finished episode 30 of the merged show
        {"item_guid": "aot30", "user_guid": "u1", "ts": 0, "watched": 1,
         "update_time": 1_700_000_200_000, "type": "Episode"},
        # bob only started ep3
        {"item_guid": "ep3", "user_guid": "u2", "ts": 100, "watched": 0,
         "update_time": 1_700_000_300_000, "type": "Episode"},
        # noise: a live channel, a movie and a drama
        {"item_guid": "live1", "user_guid": "u1", "ts": 5000, "watched": 1,
         "update_time": 1_700_000_400_000, "type": "LiveChannel"},
        {"item_guid": "movie1", "user_guid": "u1", "ts": 0, "watched": 1,
         "update_time": 1_700_000_500_000, "type": "Movie"},
        {"item_guid": "drm1", "user_guid": "u1", "ts": 0, "watched": 1,
         "update_time": 1_700_000_600_000, "type": "Episode"},
    ])
    db.commit()
    db.close()
    return path


@pytest.fixture()
def fnos_db(tmp_path) -> str:
    return build_fnos_db(str(tmp_path / "trimmedia.db"))


@pytest.fixture()
def anime_dir() -> str:
    return ANIME_DIR


def make_config(fnos_db: str, state_db: str, *, dry_run: bool = False,
                watch_ratio: float = 0.9, verify_remote: bool = True) -> Config:
    return Config(
        app=AppConfig(dry_run=dry_run, state_db=state_db, timezone="UTC", log_level="DEBUG"),
        source=SourceConfig(
            type="fnos",
            mode="snapshot",
            snapshot_path=fnos_db,
            include_paths=[ANIME_DIR],
            skip_season_zero=True,
            min_episodes=1,
            fnos=FnosOptions(),
        ),
        sync=SyncConfig(
            interval_seconds=300,
            watch_ratio=watch_ratio,
            min_position_seconds=60,
            mark_in_progress=True,
            mark_completed=True,
            verify_remote=verify_remote,
        ),
        target=TargetConfig(
            type="bangumi",
            user_agent="fnos2bgm-test/0",
            min_interval_seconds=0,
            accounts=[AccountConfig(name="main", client_id="id", client_secret="secret")],
        ),
        match=MatchConfig(),
        server=ServerConfig(enabled=False),
        users=[
            UserConfig(fnos_user="alice", account="main"),
            UserConfig(fnos_user="bob", account="main"),
        ],
    )


class FakeTracker(Tracker):
    """In-memory tracker used by the sync tests."""

    name = "fake"

    def __init__(self, account: str = "main") -> None:
        self.account = account
        self.subjects: dict[int, list[TrackEpisode]] = {}
        self.hits: dict[str, list[SubjectHit]] = {}
        self.marked: list[tuple[int, int, int]] = []
        self.collections: dict[int, int] = {}
        self.remote_marks: dict[int, dict[int, int]] = {}
        self.search_terms: list[str] = []
        self.auth_error: Exception | None = None
        self.fail_marks: Exception | None = None

    def ensure_ready(self) -> None:
        if self.auth_error:
            raise self.auth_error

    def search(self, keyword: str, limit: int = 8) -> list[SubjectHit]:
        self.search_terms.append(keyword)
        return self.hits.get(keyword, [])

    def episodes(self, subject_id: int) -> list[TrackEpisode]:
        return self.subjects.get(subject_id, [])

    def subject_name(self, subject_id: int) -> str | None:
        return "subject-%d" % subject_id

    def collection(self, subject_id: int) -> int | None:
        return self.collections.get(subject_id)

    def set_collection(self, subject_id: int, status: int) -> None:
        self.collections[subject_id] = status

    def episode_marks(self, subject_id: int) -> dict[int, int]:
        return self.remote_marks.get(subject_id, {})

    def mark_episode(self, subject_id: int, episode_id: int, status: int = EP_COLLECTED) -> None:
        if self.fail_marks:
            raise self.fail_marks
        self.marked.append((subject_id, episode_id, status))

    def last_marked(self) -> list[int]:
        return [episode_id for _subject, episode_id, _status in self.marked]


def make_episodes(count: int, start: int = 1, base_id: int = 1000) -> list[TrackEpisode]:
    return [
        TrackEpisode(episode_id=base_id + i, number=float(start + i), name="ep %d" % (start + i))
        for i in range(count)
    ]


@pytest.fixture()
def fake_tracker() -> FakeTracker:
    return FakeTracker()


def assert_auth_error(exc_info) -> None:
    assert isinstance(exc_info.value, AuthError)

from __future__ import annotations

import sqlite3

import pytest

from bangumi_syncer.config import FnosOptions, SourceConfig
from bangumi_syncer.sources.base import SourceError
from bangumi_syncer.sources.fnos import FnosSource, path_allowed


def build_source(db_path: str, **overrides) -> FnosSource:
    kwargs = dict(
        type="fnos",
        mode="snapshot",
        snapshot_path=db_path,
        include_paths=["/vol1/1000/\u5f71\u7247/\u52a8\u6f2b"],
        skip_season_zero=True,
        min_episodes=1,
        fnos=FnosOptions(),
    )
    kwargs.update(overrides)
    source = FnosSource(SourceConfig(**kwargs))
    source.prepare()
    return source


def test_users_are_read_independently_of_plays(fnos_db):
    snapshot = build_source(fnos_db).snapshot()
    # the template account is filtered, users without any play still show up
    assert {u.name for u in snapshot.users} == {"alice", "bob"}


def test_only_anime_episodes_are_kept(fnos_db):
    snapshot = build_source(fnos_db).snapshot()
    keys = {series.key for series in snapshot.series}
    assert keys == {"fnos:tv1:s1", "fnos:tv2:s1"}
    titles = {series.title for series in snapshot.series}
    assert "\u5927\u660e\u738b\u671d1566" not in titles  # drama filtered by path
    assert len(snapshot.plays) == 4  # 3 for alice, 1 for bob; live/movie/drama ignored
    user_plays = snapshot.by_user()
    assert len(user_plays["u1"]) == 3
    assert len(user_plays["u2"]) == 1


def test_season_metadata(fnos_db):
    snapshot = build_source(fnos_db).snapshot()
    series = {s.key: s for s in snapshot.series}
    pharmacy = series["fnos:tv1:s1"]
    assert pharmacy.episode_count == 24
    assert pharmacy.year == 2023
    assert pharmacy.season == 1
    assert pharmacy.aliases == ("\u85ac\u5c4b\u306e\u3072\u3068\u308a\u3054\u3068",)
    assert series["fnos:tv2:s1"].episode_count == 52


def test_specials_are_skipped_by_default(fnos_db):
    snapshot = build_source(fnos_db).snapshot()
    assert all(s.season > 0 for s in snapshot.series)
    numbers = {p.episode.number for p in snapshot.plays if p.episode.series.key == "fnos:tv1:s1"}
    assert 25 not in numbers  # the season-0 special is not in season 1


def test_specials_can_be_kept(fnos_db):
    snapshot = build_source(fnos_db, skip_season_zero=False).snapshot()
    assert {s.season for s in snapshot.series} == {0, 1}


def test_runtime_and_position_units(fnos_db):
    snapshot = build_source(fnos_db).snapshot()
    plays = {p.episode.number: p for p in snapshot.plays if p.episode.series.key == "fnos:tv1:s1"}
    assert plays[1].runtime_seconds == 24 * 60
    assert plays[1].finished_flag is True
    assert plays[2].position_seconds == 1300  # newest record wins over the older one


def test_newest_play_record_wins(fnos_db):
    snapshot = build_source(fnos_db).snapshot()
    plays = {p.episode.number: p for p in snapshot.plays if p.episode.series.key == "fnos:tv1:s1"}
    assert plays[2].position_seconds == 1300


def test_exclude_paths(fnos_db):
    snapshot = build_source(
        fnos_db, exclude_paths=["/vol1/1000/\u5f71\u7247/\u52a8\u6f2b/\u8fdb\u51fb\u7684\u5de8\u4eba"]
    ).snapshot()
    assert {s.key for s in snapshot.series} == {"fnos:tv1:s1"}


def test_min_episodes(fnos_db):
    snapshot = build_source(fnos_db, min_episodes=30).snapshot()
    assert {s.key for s in snapshot.series} == {"fnos:tv2:s1"}


def test_missing_snapshot_raises(tmp_path):
    source = FnosSource(SourceConfig(mode="snapshot", snapshot_path=str(tmp_path / "nope.db")))
    with pytest.raises(SourceError) as excinfo:
        source.prepare()
    assert "not found" in str(excinfo.value)


def test_schema_drift_raises_clear_error(tmp_path):
    path = str(tmp_path / "broken.db")
    db = sqlite3.connect(path)
    db.executescript(
        'create table "user"(guid text primary key, username text);'
        "create table item(guid text primary key, type text);"
    )
    db.commit()
    db.close()
    with pytest.raises(SourceError) as excinfo:
        FnosSource(SourceConfig(mode="snapshot", snapshot_path=path)).prepare()
    message = str(excinfo.value)
    assert "missing" in message
    assert "item.title" in message


@pytest.mark.parametrize(
    "path,includes,excludes,expected",
    [
        ("/vol1/1000/\u5f71\u7247/\u52a8\u6f2b/a/b.mp4", ["/vol1/1000/\u5f71\u7247/\u52a8\u6f2b"], [], True),
        ("/vol1/1000/\u5f71\u7247/\u7535\u89c6\u5267/a.mp4", ["/vol1/1000/\u5f71\u7247/\u52a8\u6f2b"], [], False),
        ("/vol1/1000/\u5f71\u7247/\u52a8\u6f2b/a.mp4", [], ["/vol1/1000/\u5f71\u7247/\u52a8\u6f2b"], False),
        ("vol1/1000/\u5f71\u7247/\u52a8\u6f2b/a.mp4", ["/vol1/1000/\u5f71\u7247/\u52a8\u6f2b"], [], True),
        ("", ["/vol1/1000/\u5f71\u7247/\u52a8\u6f2b"], [], False),
        ("https://example.com/live.m3u8", [], [], True),
    ],
)
def test_path_allowed(path, includes, excludes, expected):
    assert path_allowed(path, includes, excludes) is expected

from __future__ import annotations

from bangumi_syncer.models import Episode, PlayState, SeriesRef, is_watched

SERIES = SeriesRef(key="k", title="t", year=2023, season=1, episode_count=24)


def play(runtime_seconds=24 * 60, position=0, finished=False) -> PlayState:
    episode = Episode(series=SERIES, number=1, runtime_seconds=runtime_seconds)
    return PlayState(user_key="u1", episode=episode, position_seconds=position, finished_flag=finished)


def test_finished_flag_always_counts():
    assert is_watched(play(position=0, finished=True), 0.9, 60)


def test_ratio_threshold():
    runtime = 24 * 60
    assert is_watched(play(runtime, int(runtime * 0.9)), 0.9, 60)
    assert not is_watched(play(runtime, int(runtime * 0.9) - 1), 0.9, 60)


def test_unknown_runtime_needs_the_flag():
    assert not is_watched(play(runtime_seconds=None, position=9999), 0.9, 60)
    assert not is_watched(play(runtime_seconds=0, position=9999), 0.9, 60)
    assert is_watched(play(runtime_seconds=None, finished=True), 0.9, 60)


def test_minimum_position_guard():
    assert not is_watched(play(runtime_seconds=60, position=59), 0.9, 60)
    assert is_watched(play(runtime_seconds=60, position=60), 0.9, 60)


def test_zero_ratio_disables_ratio_matching():
    assert not is_watched(play(position=9999), 0.0, 60)


def test_series_search_terms_dedup_and_order():
    series = SeriesRef(
        key="k",
        title="\u83ef\u8a9e",
        original_title="\u65e5\u672c\u8a9e",
        aliases=("\u65e5\u672c\u8a9e", "\u82f1\u8a9e"),
    )
    assert series.search_terms() == ["\u83ef\u8a9e", "\u65e5\u672c\u8a9e", "\u82f1\u8a9e"]

from __future__ import annotations

import pytest

from fnos2bgm.config import MatchConfig
from fnos2bgm.matcher import (
    Matcher,
    effective_season,
    normalize_title,
    parse_season_hint,
    strip_season_marker,
)
from fnos2bgm.models import SeriesRef
from fnos2bgm.trackers.base import SubjectHit

PHARMACY_CN = "\u836f\u5c4b\u5c11\u5973\u7684\u5462\u5583"
PHARMACY_JP = "\u85ac\u5c4b\u306e\u3072\u3068\u308a\u3054\u3068"


def series(title: str, season: int = 1, year: int | None = 2023, eps: int = 24,
           original: str | None = None) -> SeriesRef:
    return SeriesRef(key="k", title=title, original_title=original, year=year,
                     season=season, episode_count=eps)


def hit(subject_id: int, name: str, name_cn: str = "", year: int | None = None,
        eps: int | None = None) -> SubjectHit:
    return SubjectHit(subject_id=subject_id, name=name, name_cn=name_cn, year=year, episode_count=eps)


@pytest.mark.parametrize(
    "title,expected",
    [
        (PHARMACY_JP + " \u7b2c2\u671f", 2),
        (PHARMACY_JP + " \u7b2c3\u671f \u7b2c2\u30af\u30fc\u30eb", 3),
        ("\u836f\u5c4b\u5c11\u5973\u7684\u5462\u5583 \u7b2c\u4e8c\u5b63", 2),
        ("Some Anime Season 3", 3),
        ("Some Anime 2nd Season", 2),
        ("[TV] Show S02", None),
        ("\u30ab\u30fc\u30c9\u30ad\u30e3\u30d7\u30bf\u30fc\u3055\u304f\u3089", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_season_hint(title, expected):
    assert parse_season_hint(title) == expected


def test_strip_season_marker():
    assert strip_season_marker(PHARMACY_CN + " \u7b2c\u4e8c\u5b63").strip() == PHARMACY_CN
    assert strip_season_marker("Show Season 2").strip() == "Show"


def test_normalize_title_makes_season_variants_equal():
    assert normalize_title(PHARMACY_CN + " \u7b2c\u4e8c\u5b63") == normalize_title(PHARMACY_CN)
    assert normalize_title("Re:ZERO") == normalize_title("re zero")
    assert normalize_title("[TV] Show") == normalize_title("Show")


def test_effective_season_uses_title_when_fnOS_says_season_one():
    assert effective_season(series("Show", season=1)) == 1
    assert effective_season(series("Show", season=2)) == 2
    assert effective_season(series("Show \u7b2c\u4e8c\u5b63", season=1)) == 2


def test_exact_match_wins():
    matcher = Matcher()
    decision = matcher.decide(series(PHARMACY_CN, year=2023, eps=24),
                              [hit(420628, PHARMACY_JP, PHARMACY_CN, 2023, 24)])
    assert decision.matched
    assert decision.hit is not None and decision.hit.subject_id == 420628
    assert decision.score >= 0.95


def test_second_season_is_not_confused_with_the_first():
    """fnOS reports the show's first_air_date, so the year must not veto season 2."""
    matcher = Matcher()
    candidates = [
        hit(301541, "\u30c8\u30cb\u30ab\u30af\u30ab\u30ef\u30a4\u30a4", "\u603b\u4e4b\u5c31\u662f\u975e\u5e38\u53ef\u7231", 2020, 12),
        hit(355798, "\u30c8\u30cb\u30ab\u30af\u30ab\u30ef\u30a4\u30a4 \u7b2c2\u671f",
            "\u603b\u4e4b\u5c31\u662f\u975e\u5e38\u53ef\u7231 \u7b2c\u4e8c\u5b63", 2023, 12),
    ]
    decision = matcher.decide(series("\u603b\u4e4b\u5c31\u662f\u975e\u5e38\u53ef\u7231", season=2, year=2020, eps=12),
                              candidates)
    assert decision.matched
    assert decision.hit is not None and decision.hit.subject_id == 355798


def test_different_show_is_rejected():
    matcher = Matcher()
    decision = matcher.decide(series("\u7edd\u5bf9\u6743\u529b", year=2003, eps=27),
                              [hit(178799, "\u7d76\u5bfe\u9818\u57df\u6975\u6226\u7d00", "\u7edd\u5bf9\u9886\u57df\u6781\u6218\u7eaa", 2016, 10)])
    assert not decision.matched
    assert "min_score" in decision.reason


def test_partial_title_is_rejected():
    matcher = Matcher()
    decision = matcher.decide(
        series("\u8336\u554a\u4e8c\u4e2d\u6ce1\u9762\u756a", year=2016, eps=21),
        [hit(148295, "\u8336\u554a\u4e8c\u4e2d", "\u8336\u554a\u4e8c\u4e2d", 2014, 10)],
    )
    assert not decision.matched


def test_ambiguous_candidates_are_not_auto_mapped():
    matcher = Matcher(MatchConfig(min_score=0.5, min_margin=0.06))
    twins = [hit(1, PHARMACY_JP, PHARMACY_CN, 2023, 24), hit(2, PHARMACY_JP, PHARMACY_CN, 2023, 24)]
    decision = matcher.decide(series(PHARMACY_CN, year=2023, eps=24), twins)
    assert not decision.matched
    assert "ambiguous" in decision.reason


def test_no_candidates():
    decision = Matcher().decide(series(PHARMACY_CN), [])
    assert not decision.matched
    assert decision.candidates == []


def test_candidates_are_ranked_and_trimmed():
    matcher = Matcher(MatchConfig(search_limit=2))
    decision = matcher.decide(series(PHARMACY_CN, eps=24), [
        hit(1, "unrelated", "", 1999, 1),
        hit(420628, PHARMACY_JP, PHARMACY_CN, 2023, 24),
        hit(3, "another", "", 2001, 2),
    ])
    assert len(decision.candidates) <= 2
    assert decision.candidates[0]["subject_id"] == 420628


def test_score_is_clamped():
    matcher = Matcher()
    value = matcher.score(series(PHARMACY_CN, year=2023, eps=24),
                          hit(420628, PHARMACY_JP, PHARMACY_CN, 2023, 24))
    assert 0.0 <= value <= 1.0


def test_aliases_help_matching():
    matcher = Matcher()
    local = SeriesRef(key="k", title="unknown local name", aliases=(PHARMACY_JP,), year=2023, episode_count=24)
    decision = matcher.decide(local, [hit(420628, PHARMACY_JP, PHARMACY_CN, 2023, 24)])
    assert decision.matched

"""Title matching between a local season and a Bangumi subject.

fnOS scrapes TMDB titles; Bangumi names its anime in Japanese with optional
Chinese titles. There is no id shared by both systems (``external_ids`` is
always empty in trim.media), so matching is heuristic:

1. normalise titles and strip season markers ("第二季", "Season 3", "第2期");
2. compare against both ``name`` and ``name_cn`` (plus local TMDB aliases);
3. adjust for release year, episode count and the season number encoded in the
   candidate title;
4. only auto-map when the best score is high enough *and* clearly ahead of the
   runner-up. Everything else goes to a review queue where a human picks the
   right subject with ``bangumi-syncer map add``.

This is deliberately conservative: a wrong mapping writes wrong watch history,
a missing mapping just waits one more day for a human decision.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass, field

from .config import MatchConfig
from .log import get_logger
from .models import SeriesRef
from .trackers.base import SubjectHit

log = get_logger("matcher")

CN_DIGITS = {
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6,
    "七": 7, "八": 8, "九": 9, "十": 10,
}

# Titles that only differ by these markers should be treated as the same show.
SEASON_MARKER_PATTERNS = (
    r"第\s*(?:[0-9]{1,2}|[一二三四五六七八九十]{1,2})\s*[季期部]",
    r"第\s*[0-9]{1,2}\s*クール",
    r"(?i)season\s*[0-9]{1,2}",
    r"(?i)[0-9]{1,2}(?:st|nd|rd|th)\s*season",
    r"(?:^|[\s\-_～~])[0-9]{1,2}\s*期",
)

_PAREN = re.compile(r"[\(（\[【][^\)）\]】]*[\)）\]】]")
_NON_WORD = re.compile(r"[^\w\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+", re.UNICODE)
_SEASON_STRIP = [re.compile(p) for p in SEASON_MARKER_PATTERNS]


def _cn_to_int(text: str) -> int | None:
    if text.isdigit():
        return int(text)
    if text == "十":
        return 10
    if text.startswith("十"):
        return 10 + CN_DIGITS.get(text[1:], 0) if len(text) == 2 else 10
    if "十" in text:
        head, _, tail = text.partition("十")
        return (CN_DIGITS.get(head, 0) or 1) * 10 + (CN_DIGITS.get(tail, 0) if tail else 0)
    return CN_DIGITS.get(text)


def parse_season_hint(name: str | None) -> int | None:
    """Return the season number encoded in a title, if any."""
    if not name:
        return None
    text = unicodedata.normalize("NFKC", name)
    for pattern in (
        r"第\s*([0-9]{1,2}|[一二三四五六七八九十]{1,2})\s*[季期部]",
        r"(?i)season\s*([0-9]{1,2})",
        r"(?i)([0-9]{1,2})(?:st|nd|rd|th)\s*season",
        r"(?:^|[\s\-_～~])([0-9]{1,2})\s*期",
    ):
        match = re.search(pattern, text)
        if match:
            return _cn_to_int(match.group(1))
    return None


def strip_season_marker(name: str) -> str:
    text = unicodedata.normalize("NFKC", name or "")
    for pattern in _SEASON_STRIP:
        text = pattern.sub(" ", text)
    return text


def normalize_title(name: str | None) -> str:
    """Lowercase, drop punctuation/bracketed noise and season markers."""
    if not name:
        return ""
    text = strip_season_marker(name)
    text = _PAREN.sub(" ", text)
    text = unicodedata.normalize("NFKC", text).lower()
    text = text.replace("&", "and").replace("＆", "and")
    text = _NON_WORD.sub("", text)
    return text


def effective_season(series: SeriesRef) -> int:
    """fnOS reports ``Season 1`` even when the show folder says "第二季"."""
    if series.season and series.season > 1:
        return series.season
    hinted = parse_season_hint(series.title) or parse_season_hint(series.original_title)
    return hinted or 1


@dataclass(slots=True)
class Decision:
    hit: SubjectHit | None
    score: float
    candidates: list[dict] = field(default_factory=list)
    reason: str = ""

    @property
    def matched(self) -> bool:
        return self.hit is not None


class Matcher:
    def __init__(self, cfg: MatchConfig | None = None) -> None:
        self.cfg = cfg or MatchConfig()

    # --------------------------------------------------------------- scoring
    def title_score(self, series: SeriesRef, hit: SubjectHit) -> float:
        local_terms = [normalize_title(t) for t in series.search_terms()]
        local_terms = [t for t in local_terms if t]
        remote_names = [normalize_title(n) for n in (hit.name, hit.name_cn) if n]
        remote_names = [n for n in remote_names if n]
        best = 0.0
        for local in local_terms:
            for remote in remote_names:
                if local == remote:
                    score = 1.0
                elif local in remote or remote in local:
                    ratio = min(len(local), len(remote)) / max(len(local), len(remote))
                    score = 0.90 * ratio
                else:
                    score = difflib.SequenceMatcher(None, local, remote).ratio() * 0.92
                best = max(best, score)
        return best

    def score(self, series: SeriesRef, hit: SubjectHit) -> float:
        base = self.title_score(series, hit)
        if base <= 0:
            return 0.0

        # Season: candidate titles encode it; unmarked titles are season 1.
        local_season = effective_season(series)
        hint = parse_season_hint(hit.name) or parse_season_hint(hit.name_cn) or 1
        if hint == local_season:
            if local_season > 1:
                base += 0.04
        else:
            base -= 0.18

        # fnOS stores the *show's* first air date on the TV item, not the air
        # date of season N, so the year is a weak signal once we are past
        # season 1 (and a strong one for the first season).
        year_weight = 0.5 if local_season > 1 else 1.0
        if series.year and hit.year:
            diff = abs(series.year - hit.year)
            if diff == 0:
                base += 0.05 * year_weight
            elif diff == 1:
                base += 0.02 * year_weight
            elif diff <= 2:
                base -= 0.04 * year_weight
            else:
                base -= 0.15 * year_weight

        if series.episode_count and hit.episode_count:
            ratio = min(series.episode_count, hit.episode_count) / max(series.episode_count, hit.episode_count)
            if ratio >= 0.99:
                base += 0.04
            elif ratio >= 0.8:
                base += 0.02
            elif ratio >= 0.5:
                base -= 0.03
            else:
                base -= 0.08

        return max(0.0, min(1.0, base))

    # -------------------------------------------------------------- decision
    def decide(self, series: SeriesRef, hits: list[SubjectHit]) -> Decision:
        scored: list[tuple[float, SubjectHit]] = [(self.score(series, hit), hit) for hit in hits]
        scored.sort(key=lambda item: item[0], reverse=True)
        candidates = [
            {
                "subject_id": hit.subject_id,
                "name": hit.name,
                "name_cn": hit.name_cn,
                "year": hit.year,
                "eps": hit.episode_count,
                "score": round(score, 3),
            }
            for score, hit in scored[: self.cfg.search_limit]
        ]
        if not scored:
            return Decision(None, 0.0, candidates, "no search results")
        best_score, best_hit = scored[0]
        if best_score < self.cfg.min_score:
            return Decision(
                None,
                best_score,
                candidates,
                "best candidate %.3f < min_score %.3f" % (best_score, self.cfg.min_score),
            )
        if len(scored) > 1:
            runner_up = scored[1][0]
            if best_score - runner_up < self.cfg.min_margin:
                return Decision(
                    None,
                    best_score,
                    candidates,
                    "ambiguous: %.3f vs %.3f" % (best_score, runner_up),
                )
        return Decision(best_hit, best_score, candidates, "matched %.3f" % best_score)

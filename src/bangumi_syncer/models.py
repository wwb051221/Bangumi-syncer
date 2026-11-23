"""Normalised data model exchanged between sources, matcher and trackers.

Everything the rest of the program sees is expressed here, so adding a new
media-server adapter only requires producing these objects.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class SeriesRef:
    """One local season of one show, as the media server sees it.

    A "series" is deliberately scoped to a *season*: fnOS models
    ``TV -> Season -> Episode`` and Bangumi models every anime season as its
    own subject, so a season is the natural unit for mapping.
    """

    key: str
    title: str
    original_title: str | None = None
    aliases: tuple[str, ...] = ()
    year: int | None = None
    season: int = 1
    episode_count: int = 0
    library_path: str | None = None

    def search_terms(self) -> list[str]:
        """Candidate search keywords, most specific first, de-duplicated."""
        terms: list[str] = []
        for t in (self.title, self.original_title, *self.aliases):
            if t and t.strip() and t.strip() not in terms:
                terms.append(t.strip())
        return terms


@dataclass(frozen=True, slots=True)
class Episode:
    series: SeriesRef
    number: int
    title: str | None = None
    runtime_seconds: int | None = None


@dataclass(frozen=True, slots=True)
class PlayState:
    """The media server's newest play record for one user and one episode."""

    user_key: str
    episode: Episode
    position_seconds: int = 0
    finished_flag: bool = False
    last_played_at: int = 0  # epoch seconds

    @property
    def runtime_seconds(self) -> int | None:
        return self.episode.runtime_seconds


@dataclass(frozen=True, slots=True)
class SourceUser:
    key: str  # stable identifier (guid)
    name: str  # human readable, usually the login name


@dataclass(slots=True)
class SourceSnapshot:
    """Result of one source read: users, shows, and their play states."""

    users: list[SourceUser] = field(default_factory=list)
    plays: list[PlayState] = field(default_factory=list)
    series: list[SeriesRef] = field(default_factory=list)

    def by_user(self) -> dict[str, list[PlayState]]:
        out: dict[str, list[PlayState]] = {}
        for play in self.plays:
            out.setdefault(play.user_key, []).append(play)
        return out


def is_watched(play: PlayState, watch_ratio: float, min_position_seconds: int) -> bool:
    """Decide whether a play record counts as "watched" for syncing.

    fnOS often leaves ``watched`` at 0 even when the user finished the file
    (credits skipped, player closed at the end), so the position ratio is the
    primary signal. When the duration is unknown only the explicit flag counts.
    """
    if play.finished_flag:
        return True
    runtime = play.runtime_seconds
    if not runtime or runtime <= 0:
        return False
    if watch_ratio <= 0:
        return False
    position = max(0, int(play.position_seconds or 0))
    if position < max(0, min_position_seconds):
        return False
    return position >= runtime * watch_ratio


def episode_key(series_key: str, number: int) -> str:
    return "%s#%d" % (series_key, number)

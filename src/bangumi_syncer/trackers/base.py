"""Tracker adapters (the place progress is written to).

Bangumi is built in. Anything with the same four capabilities can be plugged
in by implementing :class:`Tracker`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

# Bangumi collection types (subject level).
WISHLIST = 1
COLLECTED = 2
DOING = 3
ON_HOLD = 4
DROPPED = 5

# Bangumi episode collection types.
EP_RESET = 0
EP_COLLECTED = 2


class TrackerError(Exception):
    pass


class AuthError(TrackerError):
    """Token missing/expired/invalid - the user has to run ``bangumi-syncer oauth``."""


@dataclass(frozen=True, slots=True)
class SubjectHit:
    subject_id: int
    name: str
    name_cn: str
    year: int | None = None
    episode_count: int | None = None
    type: int = 2
    score: float | None = None

    @property
    def display_name(self) -> str:
        return self.name_cn or self.name


@dataclass(frozen=True, slots=True)
class TrackEpisode:
    episode_id: int
    number: float  # Bangumi ``sort``, 1.5 for split episodes
    name: str = ""
    name_cn: str = ""
    airdate: str | None = None  # ISO 8601 date

    @property
    def display_name(self) -> str:
        return self.name_cn or self.name


class Tracker(ABC):
    name = "base"
    account: str = ""

    @abstractmethod
    def ensure_ready(self) -> None:
        """Validate credentials, refreshing tokens when needed."""

    @abstractmethod
    def search(self, keyword: str, limit: int = 8) -> list[SubjectHit]: ...

    @abstractmethod
    def episodes(self, subject_id: int) -> list[TrackEpisode]: ...

    @abstractmethod
    def subject_name(self, subject_id: int) -> str | None: ...

    @abstractmethod
    def collection(self, subject_id: int) -> int | None:
        """Current subject collection type, or ``None`` when not collected."""

    @abstractmethod
    def set_collection(self, subject_id: int, status: int) -> None: ...

    @abstractmethod
    def episode_marks(self, subject_id: int) -> dict[int, int]:
        """``episode_id -> type`` for episodes the user already collected."""

    @abstractmethod
    def mark_episode(self, subject_id: int, episode_id: int, status: int = EP_COLLECTED) -> None: ...

    def describe(self) -> dict[str, Any]:
        return {"type": self.name, "account": self.account}

"""Tracker adapter registry."""

from __future__ import annotations

from ..config import AccountConfig, TargetConfig
from ..log import get_logger
from ..state import StateStore
from .bangumi import BangumiTracker
from .base import (
    COLLECTED,
    DOING,
    EP_COLLECTED,
    EP_RESET,
    AuthError,
    SubjectHit,
    TrackEpisode,
    Tracker,
    TrackerError,
)

log = get_logger("tracker")

TRACKERS: dict[str, type[Tracker]] = {
    BangumiTracker.name: BangumiTracker,
}


def create(account: AccountConfig, target: TargetConfig, store: StateStore) -> Tracker:
    klass = TRACKERS.get(target.type)
    if klass is None:
        raise TrackerError("unknown tracker type %r (available: %s)" % (target.type, ", ".join(sorted(TRACKERS))))
    return klass(account, target, store)  # type: ignore[call-arg]


__all__ = [
    "TRACKERS",
    "AuthError",
    "COLLECTED",
    "DOING",
    "EP_COLLECTED",
    "EP_RESET",
    "SubjectHit",
    "TrackEpisode",
    "Tracker",
    "TrackerError",
    "create",
]

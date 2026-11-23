"""Media server adapters.

A source is anything that can tell us "which user watched which episode of
which show". fnOS (飞牛影视) is the built-in one; Jellyfin / Emby / Plex
adapters can be added by implementing :class:`SourceAdapter` and registering
the class in ``sources/__init__.py``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from ..config import SourceConfig
from ..models import SourceSnapshot


class SourceError(Exception):
    """Raised when the media server data cannot be read."""


class SourceAdapter(ABC):
    name = "base"

    def __init__(self, cfg: SourceConfig) -> None:
        self.cfg = cfg

    @abstractmethod
    def prepare(self) -> None:
        """Make the underlying data readable (snapshot it, mount it, ...)."""

    @abstractmethod
    def snapshot(self) -> SourceSnapshot:
        """Return users and their newest play state per episode."""

    def describe(self) -> dict[str, Any]:
        """Small diagnostic payload for the ``dump`` command."""
        return {"type": self.name, "mode": self.cfg.mode}

"""Source adapter registry."""

from __future__ import annotations

from ..config import SourceConfig
from ..log import get_logger
from .base import SourceAdapter, SourceError
from .fnos import FnosSource

log = get_logger("source")

SOURCES: dict[str, type[SourceAdapter]] = {
    cls.name: cls for cls in (FnosSource,)
}


def create(cfg: SourceConfig) -> SourceAdapter:
    klass = SOURCES.get(cfg.type)
    if klass is None:
        raise SourceError("unknown source type %r (available: %s)" % (cfg.type, ", ".join(sorted(SOURCES))))
    return klass(cfg)


__all__ = ["SOURCES", "SourceAdapter", "SourceError", "create"]

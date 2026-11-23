"""Tiny formatting helpers shared by the CLI and the status page."""

from __future__ import annotations

from datetime import datetime

try:  # Python 3.9+
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]


def format_time(epoch_seconds: int | float | None, tz: str = "Asia/Shanghai",
                fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    if not epoch_seconds:
        return "-"
    zone = None
    if ZoneInfo is not None:
        try:
            zone = ZoneInfo(tz)
        except Exception:
            zone = None
    try:
        moment = datetime.fromtimestamp(int(epoch_seconds), zone)
    except (OverflowError, OSError, ValueError):
        return "-"
    return moment.strftime(fmt)


def human_duration(seconds: float | int | None) -> str:
    if not seconds:
        return "0s"
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return "%dh%02dm" % (hours, minutes)
    if minutes:
        return "%dm%02ds" % (minutes, secs)
    return "%ds" % secs

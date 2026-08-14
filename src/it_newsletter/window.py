"""The collection window, as an absolute interval.

Sites report time in whatever zone they like, and the runner's own zone differs
between a laptop and CI. So the window is resolved once, to two timezone-aware
instants, and every comparison after that is unambiguous.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from it_newsletter.models import CollectionConfig


@dataclass(frozen=True)
class Window:
    """A half-open interval `[start, end)`. Both ends are timezone-aware."""

    start: datetime
    end: datetime

    def contains(self, moment: datetime | None) -> bool:
        if moment is None:
            return False
        return self.start <= moment < self.end

    def label(self, tz: ZoneInfo) -> str:
        """Human-readable range in the given zone, for logs and the email."""
        return (
            f"{self.start.astimezone(tz):%Y-%m-%d %H:%M} → "
            f"{self.end.astimezone(tz):%Y-%m-%d %H:%M}"
        )


def daily_window(config: CollectionConfig, *, now: datetime | None = None) -> Window:
    """The most recently closed daily window.

    The end is the latest occurrence of `window_end_hour` that has already
    passed, never one in the future. Running at 12:05 covers yesterday noon to
    today noon; running early, at 09:00, covers the window that closed
    yesterday rather than reaching into a day that is still accumulating. That
    is what keeps two runs on the same day from reporting the same article
    twice.
    """
    tz = ZoneInfo(config.timezone)
    now = now.astimezone(tz) if now else datetime.now(tz)

    end = now.replace(hour=config.window_end_hour, minute=0, second=0, microsecond=0)
    if end > now:
        end -= timedelta(days=1)
    return Window(start=end - timedelta(hours=config.window_hours), end=end)


def range_window(
    config: CollectionConfig,
    *,
    days: int | None = None,
    since: datetime | None = None,
    now: datetime | None = None,
) -> Window:
    """An arbitrary window ending now, for ad-hoc scans.

    Unlike the daily window this ends at the current instant, because a scan
    asks "what exists as of right now" rather than "what belongs to a day".
    """
    tz = ZoneInfo(config.timezone)
    end = now.astimezone(tz) if now else datetime.now(tz)
    if since is not None:
        return Window(start=since.astimezone(tz), end=end)
    if days is None:
        raise ValueError("range_window needs either days or since")
    return Window(start=end - timedelta(days=days), end=end)

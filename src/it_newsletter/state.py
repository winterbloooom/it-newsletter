"""What the pipeline has learned about each site. Machine-written, not committed.

Two questions look alike and are not. *Do I want this blog?* is the reader's,
and lives in `config/sites.csv` as `enabled`. *Is this blog still going?* is
the pipeline's, and lives here: the newest post it has seen, and how many runs
in a row it learned nothing.

Keeping them apart is what stops a daily job from rewriting a hand-edited
config file. Measured on this registry, folding the machine's answers into the
CSV changed 56 of 135 rows on a busy day, which is a commit a day and a merge
conflict every time the reader is mid-edit.

Nothing here is precious. Losing the file costs one run: every site is
collected, `last_post` is repopulated from what the fetchers see, and the
six-month rule is right again the next morning. That is why it can live in
`data/`, gitignored and restored from the CI cache rather than from git.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path

from it_newsletter.models import CollectionConfig, Site, SiteResult

STATE_FILE = "sites-state.json"

# Runs in a row a site may teach us nothing before it drops to the slow rota.
# Not one: a site can fail for an evening. Not twenty: a parser that broke in
# January should not still be costing a request every morning in March.
MAX_EMPTY_RUNS = 5


@dataclass
class SiteState:
    """One site's recorded history.

    `last_post` is the newest publication date any fetcher has *seen*, which is
    not the same as the newest one collected: a blog that posts weekly returns
    nothing on most days and is not dead.

    `empty_runs` counts consecutive attempts that produced neither an article
    nor a date. That is the signature of a parser that no longer matches, and
    it is deliberately not the same signal as a quiet blog, because the two
    need different fixes.
    """

    last_post: str | None = None
    empty_runs: int = 0

    @property
    def last_post_date(self) -> date | None:
        return date.fromisoformat(self.last_post) if self.last_post else None


@dataclass
class SiteStates:
    """The whole index, keyed by site name."""

    sites: dict[str, SiteState] = field(default_factory=dict)

    def get(self, name: str) -> SiteState:
        return self.sites.setdefault(name, SiteState())

    def is_active(self, site: Site, config: CollectionConfig, *, today: date) -> bool:
        """Whether to collect this site on an ordinary day.

        A site the reader disabled is never collected. Otherwise it is active
        until there is evidence against it: a newest post older than six
        months, or a run of attempts that learned nothing. Absence of evidence
        keeps a site active, so a registry with no state file collects
        everything rather than nothing.
        """
        if not site.enabled:
            return False
        entry = self.sites.get(site.name)
        if entry is None:
            return True
        if entry.empty_runs >= MAX_EMPTY_RUNS:
            return False
        last = entry.last_post_date
        if last is None:
            return True
        return last > today - timedelta(days=config.inactive_after_days)

    def status(self, site: Site, config: CollectionConfig, *, today: date) -> str:
        """A one-word reason, for the merged view a person reads."""
        if not site.enabled:
            return "disabled"
        entry = self.sites.get(site.name)
        if entry is None:
            return "unknown"
        if entry.empty_runs >= MAX_EMPTY_RUNS:
            return "failing"
        last = entry.last_post_date
        if last is None:
            return "unknown"
        if last > today - timedelta(days=config.inactive_after_days):
            return "active"
        return "dormant"


def path(data_dir: str) -> Path:
    from it_newsletter.store import data_root

    return data_root(data_dir) / STATE_FILE


def load(data_dir: str) -> SiteStates:
    target = path(data_dir)
    if not target.exists():
        return SiteStates()
    raw = json.loads(target.read_text(encoding="utf-8"))
    return SiteStates(sites={
        name: SiteState(**entry) for name, entry in raw.get("sites", {}).items()
    })


def save(states: SiteStates, data_dir: str) -> Path:
    target = path(data_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {"sites": {name: asdict(entry) for name, entry in sorted(states.sites.items())}}
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n",
                      encoding="utf-8")
    return target


def update(states: SiteStates, results: list[SiteResult]) -> None:
    """Fold a run's outcomes into the index.

    A failed fetch is not evidence that a blog stopped writing, so it advances
    `empty_runs` and leaves `last_post` alone. Only a date the fetcher actually
    saw moves that.
    """
    for result in results:
        entry = states.get(result.site)
        if result.newest_seen is None:
            entry.empty_runs += 1
            continue
        entry.empty_runs = 0
        seen = result.newest_seen.date().isoformat()
        if entry.last_post is None or seen > entry.last_post:
            entry.last_post = seen

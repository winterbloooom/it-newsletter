"""Which sites to visit today.

`is_active` is not stored, it is derived: a site is worth collecting until the
pipeline has evidence otherwise, and the evidence lives in `state.py`. Two
kinds count, and they mean different things. A newest post older than six
months is a blog that stopped writing. A run of attempts that learned nothing
is a parser that stopped matching. Neither is a collection failure on the day
it happens, which is why a fetch that raises never lowers anything: it shows up
in the email footer instead.

Deactivation needs a way back. Without one the rule is a trapdoor, since being
inactive is exactly what stops us from looking. So an inactive site is still
visited on a slower rota and reinstated the moment it yields a date.
"""

from __future__ import annotations

import logging
import zlib
from datetime import date

from it_newsletter.models import CollectionConfig, Site
from it_newsletter.state import SiteStates

logger = logging.getLogger(__name__)


def due_for_recheck(
    site: Site, states: SiteStates, config: CollectionConfig, *, today: date
) -> bool:
    """Whether an inactive site should be visited again today.

    Spread by name so the whole quiet tail does not land on one morning, and
    spread by a *stable* hash. Python randomizes `hash()` on strings per
    process, so the built-in one made the rota different on every run: the same
    date picked three different sets across three invocations, and a site was
    rechecked twice one week and not at all the next.
    """
    if states.is_active(site, config, today=today):
        return True
    every = max(1, config.recheck_inactive_every_days)
    offset = zlib.crc32(site.name.encode("utf-8"))
    return (today.toordinal() + offset) % every == 0


def sites_to_collect(
    sites: list[Site], states: SiteStates, config: CollectionConfig, *, today: date
) -> list[Site]:
    targets = [
        s for s in sites
        if s.enabled and due_for_recheck(s, states, config, today=today)
    ]
    active = sum(1 for s in targets if states.is_active(s, config, today=today))
    logger.info(
        "collecting %d site(s): %d active, %d on the recheck rota",
        len(targets), active, len(targets) - active,
    )
    return targets


def describe(
    sites: list[Site], states: SiteStates, config: CollectionConfig, *, today: date
) -> list[tuple[Site, str, str]]:
    """The merged human view: registry row, status, and newest post seen.

    The two files are split so a daily run never rewrites a hand-edited one.
    This is what puts them back together for a person to read.
    """
    out = []
    for site in sites:
        entry = states.sites.get(site.name)
        last = entry.last_post if entry and entry.last_post else "-"
        out.append((site, states.status(site, config, today=today), last))
    return out

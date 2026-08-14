"""Show the registry and what the pipeline has learned about it, merged.

The two are stored apart so a daily run never rewrites a hand-edited file, and
this puts them back together for a person: what is configured, what state that
site is in, and when it last published.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from it_newsletter import state
from it_newsletter.config import load_config
from it_newsletter.sites_index import describe

STATUS_ORDER = {"active": 0, "unknown": 1, "dormant": 2, "failing": 3, "disabled": 4}


def main() -> None:
    parser = argparse.ArgumentParser(description="List the sites and their collection status")
    parser.add_argument("--status", metavar="KIND", action="append",
                        choices=sorted(STATUS_ORDER),
                        help="show only these; repeatable")
    parser.add_argument("--fetcher", metavar="KIND", help="show only this fetcher")
    args = parser.parse_args()

    config = load_config()
    tz = ZoneInfo(config.settings.collection.timezone)
    today = datetime.now(tz).date()
    states = state.load(config.settings.output.data_dir)

    rows = describe(config.sites, states, config.settings.collection, today=today)
    if args.status:
        rows = [r for r in rows if r[1] in set(args.status)]
    if args.fetcher:
        rows = [r for r in rows if r[0].fetcher == args.fetcher]
    rows.sort(key=lambda r: (STATUS_ORDER[r[1]], r[0].name))

    if not rows:
        print("no sites match")
        sys.exit(1)

    print(f"{'status':<9} {'site':<24} {'fetcher':<10} {'last post':<11} note")
    print("-" * 92)
    for site, status, last in rows:
        print(f"{status:<9} {site.name:<24} {site.fetcher:<10} {last:<11} {site.note[:34]}")

    counts: dict[str, int] = {}
    for _, status, _ in rows:
        counts[status] = counts.get(status, 0) + 1
    summary = "  ".join(f"{k} {v}" for k, v in sorted(counts.items(), key=lambda kv: STATUS_ORDER[kv[0]]))
    print(f"\n{len(rows)} site(s):  {summary}")


if __name__ == "__main__":
    main()

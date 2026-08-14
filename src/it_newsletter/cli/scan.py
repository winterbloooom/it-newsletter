"""Ad-hoc range queries. Reads what is stored, crawls only what is missing.

Run locally, not by CI. It sends no email and calls no model: the point is to
reconstruct what a set of blogs published over some past range, cheaply enough
to do it on a whim.

`data/` is a cache rather than an archive, so a range that was never collected
is not lost, it just costs a crawl. Any day already stored is reused, and its
URLs are handed to the fetchers so a site whose dates live on detail pages does
not re-request articles we already hold.

Results go to `data/scan/`, kept apart from `data/daily/` so an exploratory
query can never be mistaken for the record of a day.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from it_newsletter import store
from it_newsletter.collect import collect
from it_newsletter.config import load_config
from it_newsletter.window import Window

logger = logging.getLogger(__name__)

DEFAULT_DAYS = 365


def run(
    *,
    days: int | None,
    since: datetime | None,
    last: int | None,
    only: list[str] | None,
    label: str | None,
    no_crawl: bool,
) -> int:
    config = load_config()
    settings = config.settings
    tz = ZoneInfo(settings.collection.timezone)
    now = datetime.now(tz)

    start = since.replace(tzinfo=tz) if since else now - timedelta(days=days or DEFAULT_DAYS)
    window = Window(start=start, end=now)
    logger.info("range: %s", window.label(tz))

    stored, missing = store.read_range(settings.output.data_dir, window.start, window.end)
    if only:
        # `--only` restricts the answer, not just the crawling. Merging in
        # stored articles from every other blog would contradict what was asked.
        stored = [a for a in stored if a.site in set(only)]
    logger.info("stored: %d article(s); %d day(s) have no file", len(stored), len(missing))

    fresh: list = []
    if missing and not no_crawl:
        targets = config.enabled_sites()
        if only:
            targets = [s for s in targets if s.name in only]
        logger.info("crawling %d site(s) to fill the gaps", len(targets))
        fresh, results = collect(
            config, window,
            sites=targets,
            known_urls={a.url for a in stored},
        )
        for result in results:
            if not result.ok:
                logger.warning("%s: %s", result.site, result.error)
    elif missing:
        logger.info("--no-crawl: reporting only what is already stored")

    articles = store.dedupe(stored + fresh)
    if last is not None:
        articles = articles[:last]

    if not articles:
        logger.warning("nothing found in this range")
        return 1

    name = label or _label(window, tz, last=last, days=days, since=since)
    path = store.write(store.scan_path(settings.output.data_dir, name), articles)

    _report(articles, path, reused=len(stored), crawled=len(fresh))
    return 0


def _label(window: Window, tz, *, last: int | None, days: int | None, since) -> str:
    """A filename that says what was asked for, not just when it was run."""
    stamp = window.end.astimezone(tz).strftime("%Y-%m-%d")
    if since:
        span = f"since{window.start.astimezone(tz):%Y%m%d}"
    else:
        span = f"last{days or DEFAULT_DAYS}d"
    return f"{stamp}-{span}" + (f"-top{last}" if last else "")


def _report(articles: list, path, *, reused: int, crawled: int) -> None:
    by_site: dict[str, int] = {}
    for article in articles:
        by_site[article.site] = by_site.get(article.site, 0) + 1

    oldest = min(a.published_at for a in articles).date()
    newest = max(a.published_at for a in articles).date()

    print(f"\n{len(articles)}건  ·  {oldest} ~ {newest}  ·  {len(by_site)}개 블로그")
    print(f"저장: {path}")
    print(f"재사용 {reused}건 / 새로 수집 {crawled}건\n")
    for site, count in sorted(by_site.items(), key=lambda kv: -kv[1]):
        print(f"  {count:>4}  {site}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Collect a past range into data/scan/. No email, no summaries.",
        epilog="예: scan.sh --days 365    scan.sh --last 30    scan.sh --since 2026-01-01",
    )
    span = parser.add_mutually_exclusive_group()
    span.add_argument("--days", type=int, metavar="D",
                      help=f"look back D days (default {DEFAULT_DAYS})")
    span.add_argument("--since", metavar="YYYY-MM-DD",
                      help="look back to this date")
    parser.add_argument("--last", type=int, metavar="N",
                        help="keep only the newest N articles of the range")
    parser.add_argument("--only", metavar="SITE", action="append",
                        help="restrict to one site by name; repeatable")
    parser.add_argument("--label", metavar="NAME",
                        help="name the output file yourself")
    parser.add_argument("--no-crawl", action="store_true",
                        help="report only what is already stored, requesting nothing")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s",
                        datefmt="%H:%M:%S")

    try:
        sys.exit(run(
            days=args.days,
            since=datetime.fromisoformat(args.since) if args.since else None,
            last=args.last,
            only=args.only,
            label=args.label,
            no_crawl=args.no_crawl,
        ))
    except Exception:
        logger.exception("scan failed")
        sys.exit(1)


if __name__ == "__main__":
    main()

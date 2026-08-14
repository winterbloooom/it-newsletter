"""Tier 1: the site publishes a standard feed.

This covers most of the registry, and for almost every site it needs no
parameter beyond the feed URL. A site reaches the later tiers only after
`discover.py` has failed to find a feed for it.

params:
    date_from  "detail" when the feed carries no dates and each article's own
               page has to be consulted. developers.googleblog.com is the one
               site in the registry that needs this: its items carry exactly
               title, link, description and guid, with no date element in any
               namespace, and no HTTP Last-Modified either. Its article pages
               do carry a JSON-LD Article block with datePublished.
"""

from __future__ import annotations

from datetime import datetime

from it_newsletter.fetchers._common import (
    Http,
    PageMeta,
    canonical_url,
    feed_items,
    html_soup,
    html_to_text,
    page_meta,
    parse_date,
    resolve_tz,
)
from it_newsletter.models import Article, FetchOutcome, Site
from it_newsletter.window import Window

# Feed descriptions run from a one-line dek to the article's whole body. Kept
# whole they would dominate the stored artifact, and the ranking model reads
# only the opening anyway.
SUBTITLE_LIMIT = 300

# How much of a feed-supplied body to carry into the summarizer. Enough to see
# what the article did, without holding a whole migration retrospective in
# memory for every article in the window.
BODY_LIMIT = 6000

# Ceiling on detail-page requests for a `date_from: detail` site, so a feed
# that suddenly returns hundreds of entries cannot turn into hundreds of
# requests against someone else's server.
MAX_DETAIL_FETCHES = 40


def fetch(
    site: Site,
    window: Window,
    *,
    http: Http,
    known_urls: set[str] | None = None,
) -> FetchOutcome:
    """Collect the site's articles that fall inside the window.

    Raises when the feed yields no entries at all. That is not the same as a
    quiet day: a feed carries the last N posts regardless of the window, so
    zero entries means the document is not the feed we think it is.
    `tech.kakaopay.com/rss.xml` is well-formed RSS with no items, and reading
    that as an empty day would hide the site for good.

    `known_urls` are articles already in the store. They are skipped before any
    detail-page request, which is what keeps a `date_from: detail` site cheap
    after its first run.
    """
    assume_tz = resolve_tz(site.tz)
    items = feed_items(http.get_bytes(site.source_url))
    if not items:
        raise ValueError(
            f"feed at {site.source_url} parsed but contains no entries; "
            f"the site likely moved its feed or serves a placeholder"
        )

    from_detail = site.params.get("date_from") == "detail"
    known = known_urls or set()

    articles: list[Article] = []
    newest_seen: datetime | None = None
    undated = 0
    detail_fetches = 0

    for item in items:
        if not item.title or not item.link:
            continue
        url = canonical_url(item.link)

        author = item.author
        # Most feeds put the whole post in content:encoded. Keep it for the
        # summarizer (it is never stored) and take the opening as the subtitle.
        body = html_to_text(item.summary, unescape=True, limit=BODY_LIMIT)
        subtitle = body[:SUBTITLE_LIMIT] or None
        published = parse_date(item.published_raw, assume_tz=assume_tz)

        if published is None and from_detail and url not in known:
            if detail_fetches >= MAX_DETAIL_FETCHES:
                break
            detail_fetches += 1
            # The request is already paid for, so take everything the page
            # offers that the feed left out. developers.googleblog.com gives up
            # an author here that its feed never mentions. The title is not
            # taken: og:title carries a site-name suffix the feed title lacks.
            meta = _detail_meta(http, url)
            if meta is not None:
                published = parse_date(meta.published_raw, assume_tz=assume_tz)
                author = author or meta.author
                subtitle = subtitle or (meta.subtitle[:SUBTITLE_LIMIT] if meta.subtitle else None)

        if published is None:
            undated += 1
            continue
        # Recorded before the window filter: this is the site's pulse, and the
        # window says nothing about whether the blog is still alive.
        if newest_seen is None or published > newest_seen:
            newest_seen = published
        if not window.contains(published):
            continue

        articles.append(Article(
            site=site.name,
            title=item.title,
            url=url,
            published_at=published,
            author=author,
            subtitle=subtitle,
            body=body,
        ))

    if undated == len(items):
        raise ValueError(
            f"feed at {site.source_url} has {len(items)} entries but none carry a "
            f"parseable date (first was {items[0].published_raw!r}). If the feed "
            f"genuinely has no dates, set params date_from to \"detail\"."
        )
    return FetchOutcome(articles=articles, newest_seen=newest_seen)


def _detail_meta(http: Http, url: str) -> PageMeta | None:
    """The article page's own metadata, or None if the page cannot be read."""
    try:
        return page_meta(html_soup(http.get_text(url)))
    except Exception:  # noqa: BLE001 - one unreachable article must not end the site
        return None

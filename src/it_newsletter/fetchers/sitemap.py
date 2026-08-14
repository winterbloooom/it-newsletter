"""Tier 3b: the site publishes no feed, but it does publish a sitemap.

A sitemap is the site's own machine-readable list of its URLs, which makes it a
better source than anything inferred from a list page: no shape to guess, no
navigation to filter out by name, and often a `lastmod` that bounds how much
has to be read. It sits below embedded JSON because it carries URLs rather than
articles, and above list scraping because the URLs are stated rather than
inferred.

It earns its place on sites whose listing is paginated behind JavaScript, where
the first page shows a handful of posts and the rest are unreachable from the
markup. figma.com is the case in hand: its blog listing yields nothing usable,
while its sitemap names 799 posts with dates.

params:
    sitemap_url    (optional) defaults to <origin>/sitemap.xml
    article_regex  (required) which paths are articles
    date_from      "lastmod" trusts the sitemap's date; "detail" (the default)
                   reads each article page. `lastmod` is a modification time,
                   not a publication time, so it is only right for sites that
                   never edit a published post.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlsplit

from it_newsletter.fetchers._common import (
    NAV_SEGMENTS,
    Http,
    canonical_url,
    html_soup,
    page_meta,
    parse_date,
    resolve_tz,
    sanitize_xml,
)
from it_newsletter.models import Article, FetchOutcome, Site
from it_newsletter.window import Window

SITEMAP_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"

# How many child sitemaps to follow from an index, and how many article pages
# to read in one run. Both bound the work a wide sitemap can demand.
MAX_CHILD_SITEMAPS = 8
MAX_DETAIL_FETCHES = 60

# `lastmod` moves when a post is edited, so an article can be modified long
# after publication but never before it. Widening the pre-filter by this much
# keeps a post whose lastmod predates the window from being pruned before its
# real date is read.
LASTMOD_SLACK = timedelta(days=3)


def fetch(
    site: Site,
    window: Window,
    *,
    http: Http,
    known_urls: set[str] | None = None,
) -> FetchOutcome:
    """Collect the site's articles inside the window, from its sitemap.

    Raises when the sitemap names no article at all, which means the pattern
    stopped matching, not that the blog stopped publishing.
    """
    params = site.params
    pattern = re.compile(params["article_regex"])
    from_lastmod = params.get("date_from") == "lastmod"
    assume_tz = resolve_tz(site.tz)
    known = known_urls or set()

    entries = _entries(http, _sitemap_url(site))
    candidates: list[tuple[str, datetime | None]] = []
    for url, lastmod_raw in entries:
        parts = urlsplit(url)
        if not pattern.search(parts.path):
            continue
        if any(seg.lower() in NAV_SEGMENTS for seg in parts.path.split("/") if seg):
            continue
        candidates.append((canonical_url(url),
                           parse_date(lastmod_raw, assume_tz=assume_tz)))

    if not candidates:
        raise ValueError(
            f"sitemap for {site.name} names no URL matching "
            f"{params['article_regex']!r}"
        )

    # Newest first, so the detail budget is spent on what the window wants.
    candidates.sort(key=lambda pair: (pair[1] is not None, pair[1]), reverse=True)

    articles: list[Article] = []
    newest_seen: datetime | None = None
    fetched = 0

    for url, lastmod in candidates:
        if from_lastmod:
            if lastmod is None:
                continue
            if newest_seen is None or lastmod > newest_seen:
                newest_seen = lastmod
            if not window.contains(lastmod):
                continue
            meta = _detail(http, url)
            if meta is None or not meta.title:
                continue
            articles.append(_article(site, url, meta, lastmod))
            continue

        # Dates come from the article pages, so the sitemap's own date is used
        # only to decide which pages are worth opening.
        if lastmod is not None and lastmod < window.start - LASTMOD_SLACK:
            break
        if url in known or fetched >= MAX_DETAIL_FETCHES:
            continue
        fetched += 1
        meta = _detail(http, url)
        if meta is None or not meta.title:
            continue
        # Nothing vouched for this URL: a sitemap lists every page the site
        # wants indexed, tag and category pages included, and figma.com names
        # those under the same /blog/<slug> shape as its posts. Same rule as
        # `html_list` applies, for the same reason.
        if not meta.declares_article:
            continue
        published = parse_date(meta.published_raw, assume_tz=assume_tz) or lastmod
        if published is None:
            continue
        if newest_seen is None or published > newest_seen:
            newest_seen = published
        if window.contains(published):
            articles.append(_article(site, url, meta, published))

    return FetchOutcome(articles=articles, newest_seen=newest_seen)


def _sitemap_url(site: Site) -> str:
    override = site.params.get("sitemap_url")
    if override:
        return override
    parts = urlsplit(site.source_url or site.url)
    return f"{parts.scheme}://{parts.netloc}/sitemap.xml"


def _entries(http: Http, url: str, *, depth: int = 0) -> list[tuple[str, str | None]]:
    """Every (loc, lastmod) the sitemap names, following one level of index."""
    try:
        root = ET.fromstring(sanitize_xml(http.get_bytes(url)))
    except Exception:  # noqa: BLE001 - an unreadable child must not end the run
        return []

    if root.tag.endswith("sitemapindex"):
        if depth:
            return []
        children = [
            child.findtext(f"{SITEMAP_NS}loc")
            for child in root.findall(f"{SITEMAP_NS}sitemap")
        ]
        out: list[tuple[str, str | None]] = []
        for child in [c for c in children if c][:MAX_CHILD_SITEMAPS]:
            out.extend(_entries(http, child, depth=1))
        return out

    return [
        (loc, entry.findtext(f"{SITEMAP_NS}lastmod"))
        for entry in root.findall(f"{SITEMAP_NS}url")
        if (loc := entry.findtext(f"{SITEMAP_NS}loc"))
    ]


def _detail(http: Http, url: str):
    try:
        return page_meta(html_soup(http.get_text(url)))
    except Exception:  # noqa: BLE001
        return None


def _article(site: Site, url: str, meta, published: datetime) -> Article:
    title = meta.title
    strip_suffix = site.params.get("title_strip")
    if strip_suffix and title.endswith(strip_suffix):
        title = title[: -len(strip_suffix)].strip()
    return Article(
        site=site.name,
        title=title,
        url=url,
        published_at=published,
        author=meta.author,
        subtitle=meta.subtitle,
    )

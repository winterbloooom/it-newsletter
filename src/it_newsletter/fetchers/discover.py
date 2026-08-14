"""Work out how a site can be collected, and report the registry row for it.

This is the `webpage-parser` skill's tier list as code. Point it at a blog's URL
and it works down the tiers in order, stopping at the first that yields dated
entries, then prints a row ready to paste into `config/sites.csv`.

It exists so that adding a site is a two-minute check rather than an
investigation, and so the same probe can be re-run when a live site goes quiet.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

from it_newsletter.fetchers._common import (
    Http,
    NAV_SEGMENTS,
    feed_items,
    html_soup,
    page_meta,
    parse_date,
    resolve_tz,
)

# Paths worth trying on any host. Ordered by how often they hit. The blog/ and
# news/ prefixes are here because a company site often keeps its feed one level
# in from the page a human would bookmark: openai.com/research has no feed, and
# openai.com/news/rss.xml carries the research posts too.
GENERIC_FEED_PATHS = (
    "feed", "rss", "rss.xml", "feed.xml", "atom.xml", "index.xml",
    "feed/", "rss/", "feeds/posts/default", "feed.json",
    "blog/feed", "blog/rss.xml", "blog/index.xml",
    "news/rss.xml", "news/feed",
)

_FEED_LINK_TYPES = ("application/rss+xml", "application/atom+xml", "application/feed+json")


@dataclass
class Finding:
    """What discovery concluded, and everything it ruled out getting there."""

    fetcher: str | None = None
    source_url: str | None = None
    params: dict = field(default_factory=dict)
    entries: int = 0
    dated: int = 0
    newest: str | None = None
    sample_title: str | None = None
    has_author: bool = False
    has_subtitle: bool = False
    tried: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.fetcher is not None

    def row(self, name: str, url: str, tz: str | None) -> str:
        """A `config/sites.csv` line for this site."""
        import csv
        import io

        buffer = io.StringIO()
        csv.writer(buffer).writerow([
            name, url, self.fetcher or "", self.source_url or "", tz or "",
            json.dumps(self.params, ensure_ascii=False) if self.params else "",
            "1", "", "",
        ])
        return buffer.getvalue().rstrip("\n")


def _host_specific(url: str) -> list[str]:
    """Feed locations that no generic guess finds.

    Each entry here cost a round of probing to learn. Medium is the one that
    matters most: `/<publication>/feed` answers 200 with an HTML page, so a
    checker that trusts the status code concludes there is no feed at all.
    """
    parts = urlsplit(url)
    host, path = parts.netloc, parts.path.strip("/")
    candidates = []
    if host.endswith("medium.com") and path:
        candidates.append(f"https://medium.com/feed/{path.split('/')[0]}")
    if host == "d2.naver.com":
        candidates.append("https://d2.naver.com/d2.atom")
    if path:  # Hugo and similar section feeds
        candidates.append(urljoin(url.rstrip("/") + "/", "index.xml"))
    return candidates


def _validate_feed(http: Http, feed_url: str, *, tz: str | None) -> Finding | None:
    """Accept a feed only if it has entries and at least one parseable date.

    Both checks are load-bearing. `tech.kakaopay.com/rss.xml` is well-formed
    RSS with zero items, and developers.googleblog.com has twenty items and no
    dates at all. Either would otherwise be registered as working and then
    report nothing, every day, silently.
    """
    try:
        items = feed_items(http.get_bytes(feed_url))
    except Exception:  # noqa: BLE001 - a candidate that is not a feed is normal
        return None
    if not items:
        return None

    assume_tz = resolve_tz(tz)
    dates = [parse_date(item.published_raw, assume_tz=assume_tz) for item in items]
    dated = [d for d in dates if d is not None]

    finding = Finding(
        fetcher="feed",
        source_url=feed_url,
        entries=len(items),
        dated=len(dated),
        newest=max(dated).isoformat() if dated else None,
        sample_title=items[0].title or None,
        has_author=any(item.author for item in items),
        has_subtitle=any(item.summary for item in items),
    )
    if not dated:
        # The feed is real but dateless. Usable only if the article pages
        # carry a date, so check one before recommending the extra requests.
        first_link = next((item.link for item in items if item.link), None)
        if first_link and _detail_has_date(http, first_link, tz=tz):
            finding.params = {"date_from": "detail"}
            finding.dated = -1  # marker: dated via detail pages
            return finding
        return None
    return finding


def _detail_has_date(http: Http, url: str, *, tz: str | None) -> bool:
    try:
        meta = page_meta(html_soup(http.get_text(url)))
    except Exception:  # noqa: BLE001
        return False
    return parse_date(meta.published_raw, assume_tz=resolve_tz(tz)) is not None


def _declared_feeds(html: str, base: str) -> list[str]:
    soup = html_soup(html)
    out = []
    for link in soup.find_all("link", href=True):
        rel = " ".join(link.get("rel") or [])
        if link.get("type") in _FEED_LINK_TYPES or "alternate" in rel.lower():
            if link.get("type") in _FEED_LINK_TYPES:
                out.append(urljoin(base, link["href"]))
    return out


def _embedded_signals(html: str) -> dict | None:
    """Which embedded-JSON shape yields an article list, if any.

    Presence of a payload is not the test. Almost every site built on Next.js
    ships a `__NEXT_DATA__` script or a streaming RSC payload, and most of them
    keep no article list in it: the first version of this function accepted the
    marker alone, and eight of ten sites it sent to the embedded fetcher failed
    outright at collection time. So the extraction is actually run here, and a
    shape counts only when records come out.
    """
    from it_newsletter.fetchers.embedded import _extract_payload, _find_records, _keys_for

    keys = _keys_for({})
    for source in ("next_data", "rsc", "js_literal"):
        try:
            payload = _extract_payload(html, source, {})
        except Exception:  # noqa: BLE001 - an absent shape is the normal case
            continue
        if payload is None:
            continue
        records = _find_records(payload, keys)
        if records:
            return {"source": source, "_records": len(records)}
    return None


def discover(url: str, *, tz: str | None = None, http: Http | None = None) -> Finding:
    """Work through the tiers for one site and report what it found.

    One failed probe is never the answer: every tier tries several candidates,
    and every tier is tried before the site is reported unparseable.
    """
    http = http or Http(timeout=25, delay=0.3)
    finding = Finding()
    base = url if url.endswith("/") else url + "/"
    origin = "{0.scheme}://{0.netloc}/".format(urlsplit(url))

    # Tier 1a: what the page declares about itself.
    html = ""
    try:
        html = http.get_text(url)
    except Exception as e:  # noqa: BLE001
        finding.tried.append(f"GET {url} -> {type(e).__name__}")

    candidates: list[str] = []
    if html:
        candidates += _declared_feeds(html, url)
    # Tier 1b and 1c: host-specific forms, then generic guesses.
    candidates += _host_specific(url)
    candidates += [urljoin(base, p) for p in GENERIC_FEED_PATHS]
    if origin != base:
        candidates += [urljoin(origin, p) for p in GENERIC_FEED_PATHS]

    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        result = _validate_feed(http, candidate, tz=tz)
        if result is not None:
            result.tried = finding.tried + [f"feed found after {len(seen)} candidates"]
            return result
        finding.tried.append(f"no feed: {candidate}")

    if not html:
        return finding

    # Tier 2: JSON the page already carries.
    signals = _embedded_signals(html)
    if signals:
        found = signals.pop("_records", 0)
        finding.fetcher = "embedded"
        finding.source_url = url
        finding.params = signals
        finding.entries = found
        finding.tried.append(f"embedded JSON: {signals['source']}, {found} record(s)")
        return finding
    finding.tried.append("no embedded record list")

    # Tier 3: anchors plus dates. Report the shape so a human can refine it.
    shapes = _anchor_shapes(html, url)
    if shapes:
        regex = _best_shape(http, html, shapes, url, tz=tz)
        dated = _detail_pages_are_dated(http, html, regex, url, tz=tz)
        finding.fetcher = "html_list"
        finding.source_url = url
        finding.params = {
            "article_regex": regex,
            "date_from": "detail" if dated else "list",
        }
        finding.dated = dated
        finding.entries = shapes[0][1]
        finding.tried.append(f"anchor shapes: {shapes[:3]}, detail-dated={dated}")
    return finding


SAMPLE_PAGES = 3
SHAPES_TO_TRY = 3


def _best_shape(
    http: Http, html: str, shapes: list[tuple[str, int]], base: str, *, tz: str | None
) -> str:
    """Pick the URL shape whose pages are actually articles.

    Frequency alone picks navigation. A blog's chrome repeats on every page
    while its posts appear once each, so the most common shape on
    cohere.com is `/{section}` and on upstage.ai it is the list page itself.
    Sampling two pages per candidate and asking whether they read as articles
    costs a handful of requests during discovery and settles it.
    """
    scored: list[tuple[int, int, str]] = []
    for regex, count in shapes[:SHAPES_TO_TRY]:
        urls = _matching_urls(html, regex, base)[:2]
        if not urls:
            # A shape that matches nothing but the page we are standing on
            # cannot be the article shape, however often it appears. This is
            # what `^/blog/?$` was on upstage.ai: the nav link to the listing,
            # repeated enough times to win on frequency alone.
            continue
        hits = 0
        for candidate in urls:
            try:
                meta = page_meta(html_soup(http.get_text(candidate)))
            except Exception:  # noqa: BLE001
                continue
            if meta.declares_article or parse_date(meta.published_raw, assume_tz=resolve_tz(tz)):
                hits += 1
        scored.append((hits, count, regex))

    scored.sort(reverse=True)
    return scored[0][2] if scored else shapes[0][0]


def _matching_urls(html: str, regex: str, base: str) -> list[str]:
    pattern = re.compile(regex)
    host = urlsplit(base).netloc
    out: list[str] = []
    for anchor in html_soup(html).find_all("a", href=True):
        absolute = urljoin(base, anchor["href"])
        parts = urlsplit(absolute)
        if parts.netloc != host or not pattern.search(parts.path):
            continue
        if parts.path.rstrip("/") == urlsplit(base).path.rstrip("/"):
            continue
        if absolute not in out:
            out.append(absolute)
    return out


def _detail_pages_are_dated(
    http: Http, html: str, regex: str, base: str, *, tz: str | None
) -> bool:
    """Whether the article pages know their own dates. This decides `date_from`.

    `detail` is preferred wherever it works, because a date read from the
    article itself belongs to that article, while a date read from a list page
    is whatever sat nearest the link. `list` is the fallback for sites that
    print the date only there: engineering.ab180.co marks every post
    `og:type: article` and dates none of them.

    The distinction matters beyond the date, because `html_list` only demands
    that a page declare itself an article when it is trusting that page for the
    date. Deciding by sampling real pages, rather than by counting dates near
    links, is what keeps figma.com's category pages out: they sit next to dates
    on the list, so a card-counting test called them articles.
    """
    from it_newsletter.fetchers.html_list import _list_entries

    entries = _list_entries(html_soup(html), re.compile(regex), base,
                            assume_tz=resolve_tz(tz))
    dated = 0
    for candidate, _ in entries[:SAMPLE_PAGES]:
        if _detail_has_date(http, candidate, tz=tz):
            dated += 1
    return dated * 2 > min(len(entries), SAMPLE_PAGES)


def _anchor_shapes(html: str, base: str) -> list[tuple[str, int]]:
    """Group the page's links by path shape, most common first.

    The most repeated same-origin shape on a list page is almost always the
    article link, which gives a starting `article_regex` to refine by hand.
    """
    from collections import Counter

    soup = html_soup(html)
    host = urlsplit(base).netloc
    counter: Counter[str] = Counter()
    for anchor in soup.find_all("a", href=True):
        absolute = urljoin(base, anchor["href"])
        if urlsplit(absolute).netloc != host:
            continue
        path = urlsplit(absolute).path
        segments = [s for s in path.split("/") if s]
        if not segments or any(s.lower() in NAV_SEGMENTS for s in segments):
            continue

        # Generalize the slug but keep a file extension: `/foo/bar.html` is a
        # far more precise regex than `/foo/bar`, and the extension is exactly
        # what distinguishes an article from a section on those sites.
        shape_parts = []
        for segment in segments:
            stem, dot, extension = segment.rpartition(".")
            if dot and len(extension) <= 5 and extension.isalpha():
                shape_parts.append(rf"[^/]+\.{extension}")
            elif len(segment) >= 6 and any(c.isdigit() or c.isalpha() for c in segment):
                shape_parts.append("[^/]+")
            else:
                shape_parts.append(re.escape(segment))
        # Anchored to the whole path. The fetcher matches with `search`, so an
        # unanchored shape like `/[^/]+` matches every path on the site and the
        # pattern stops meaning anything: cohere.com collected `/products`
        # alongside its posts.
        shape = "^/" + "/".join(shape_parts) + "/?$"
        if len(segments) <= 4:
            counter[shape] += 1
    return [(shape, n) for shape, n in counter.most_common(8) if n >= 3]

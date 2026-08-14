"""Tier 3: no feed and no embedded JSON, so read the list page and the articles.

Config-driven rather than site-specific. A site reaches here only when
`discover.py` has exhausted tiers 1 and 2, and what it needs is a URL pattern,
not code.

Two shapes are supported, and the difference is where the date lives:

  date_from "list"    the list cards carry dates, so the window can be applied
                      before any detail request. 카카오모빌리티 shows 2026.07.22
                      on each card.
  date_from "detail"  only the article page knows its own date, so every
                      candidate costs a request. 카카오페이 is this shape: its
                      date is the text of a <time> element and its subtitle
                      exists only as og:description.

params:
    article_regex  (required) matches the path of an article URL
    date_from      "list" or "detail" (default "detail")
    page_url       (optional) template with {page}, for paging back through
                   history during a scan. Without it only one page is read.
    max_articles   (optional) ceiling on detail requests per page (default 40)
"""

from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import urljoin, urlsplit

from it_newsletter.fetchers._common import (
    Http,
    NAV_SEGMENTS,
    canonical_url,
    html_soup,
    page_meta,
    parse_date,
    resolve_tz,
)
from it_newsletter.models import Article, FetchOutcome, Site
from it_newsletter.window import Window

DEFAULT_MAX_ARTICLES = 40

# How many consecutive out-of-window articles end the paging. Stopping at the
# first one assumes a strictly newest-first listing, and that assumption fails:
# figma.com/blog/engineering mixes evergreen posts among recent ones, so a
# single old entry near the top truncated the whole site to nothing.
EXHAUST_RUN = 6

# How far up from a link to look for its date. A card's date can sit on any
# ancestor, but past about four levels the search starts finding a sibling
# article's date, or the sidebar's.
_CARD_DEPTH = 4


def fetch(
    site: Site,
    window: Window,
    *,
    http: Http,
    known_urls: set[str] | None = None,
    max_pages: int = 1,
) -> FetchOutcome:
    """Collect the site's articles inside the window.

    Raises when the list page yields no matching links at all, which means the
    layout changed and `article_regex` no longer matches anything. Returning an
    empty list there would be indistinguishable from a quiet day.
    """
    params = site.params
    pattern = re.compile(params["article_regex"])
    from_list = params.get("date_from", "detail") == "list"
    max_articles = params.get("max_articles", DEFAULT_MAX_ARTICLES)
    assume_tz = resolve_tz(site.tz)
    known = known_urls or set()

    articles: list[Article] = []
    newest_seen: datetime | None = None
    seen: set[str] = set()
    total_candidates = 0

    for page in range(1, max_pages + 1):
        list_url = _page_url(site, page)
        if list_url is None:
            break
        entries = _list_entries(html_soup(http.get_text(list_url)), pattern,
                                list_url, assume_tz=assume_tz)
        total_candidates += len(entries)
        if not entries:
            break

        # `date_from` declares where the truth about a date is, and card dates
        # are only the truth in list mode. In detail mode the walk up from an
        # anchor still finds *a* date on a dense page, just not this article's,
        # so those values are discarded rather than trusted.
        if not from_list:
            entries = [(url, None) for url, _ in entries]

        for _, card_date in entries:
            if card_date is not None and (newest_seen is None or card_date > newest_seen):
                newest_seen = card_date

        # A page whose cards reach back past the window start is the last one
        # worth reading, because list pages run newest first. Checked on the
        # cards rather than on what survived the window filter: otherwise a
        # page holding only older posts would look empty rather than finished,
        # and paging would continue through the site's whole history.
        reached_past = any(d is not None and d < window.start for _, d in entries)

        candidates = entries
        if from_list:
            candidates = [c for c in candidates if window.contains(c[1])]
        candidates = [c for c in candidates if c[0] not in seen][:max_articles]

        exhausted = reached_past
        consecutive_old = 0
        for url, list_date in candidates:
            seen.add(url)
            if url in known:
                continue
            article = _build(http, site, url, list_date, assume_tz=assume_tz)
            if article is None:
                continue
            if newest_seen is None or article.published_at > newest_seen:
                newest_seen = article.published_at
            if not window.contains(article.published_at):
                # Same reasoning, for a site whose dates live on the detail
                # page and so cannot be judged from the list. A run of them,
                # not a single one, because the order is the site's to choose.
                if article.published_at < window.start:
                    consecutive_old += 1
                    if consecutive_old >= EXHAUST_RUN:
                        exhausted = True
                        break
                continue
            consecutive_old = 0
            articles.append(article)
        if exhausted:
            break

    if total_candidates == 0:
        raise ValueError(
            f"no links on {site.source_url} matched article_regex "
            f"{params['article_regex']!r}; the site's layout likely changed"
        )
    return FetchOutcome(articles=articles, newest_seen=newest_seen)


def _page_url(site: Site, page: int) -> str | None:
    if page == 1:
        return site.source_url
    template = site.params.get("page_url")
    return template.format(page=page) if template else None


def _list_entries(
    soup, pattern: re.Pattern[str], base: str, *, assume_tz
) -> list[tuple[str, object]]:
    """Article links on the list page, each with the date printed nearest it."""
    host = urlsplit(base).netloc
    out: list[tuple[str, object]] = []
    seen: set[str] = set()

    for anchor in soup.find_all("a", href=True):
        absolute = urljoin(base, anchor["href"])
        parts = urlsplit(absolute)
        if parts.netloc != host or not pattern.search(parts.path):
            continue
        # An inferred pattern matches author and tag pages too, and those are
        # dated and titled just like posts, so nothing downstream rejects them.
        segments = [seg for seg in parts.path.split("/") if seg]
        if any(seg.lower() in NAV_SEGMENTS for seg in segments):
            continue
        # A listing is not one of the things it lists. `index.html` and the
        # list page's own URL both match a pattern drawn from that page.
        if segments and segments[-1].split(".")[0].lower() == "index":
            continue
        if parts.path.rstrip("/") == urlsplit(base).path.rstrip("/"):
            continue
        url = canonical_url(absolute)
        if url in seen:
            continue
        seen.add(url)

        # The anchor's own text first, for cards wrapped entirely in a link,
        # then upward for layouts where only the headline is linked.
        published = parse_date(anchor.get_text(" ", strip=True), assume_tz=assume_tz)
        node = anchor
        for _ in range(_CARD_DEPTH):
            if published is not None:
                break
            node = node.parent
            if node is None:
                break
            published = parse_date(node.get_text(" ", strip=True), assume_tz=assume_tz)
        out.append((url, published))
    return out


def _build(http: Http, site: Site, url: str, list_date, *, assume_tz) -> Article | None:
    """Read the article page and merge it with whatever the list already gave.

    Returns None for a page that matched the URL pattern without being an
    article. An inferred `article_regex` is a shape, and shapes over-match:
    figma.com/blog/[^/]+ catches `/blog/engineering/` alongside real posts, and
    without this check the digest filled up with entries titled "All Posts |
    Figma Blog".

    The evidence required depends on where the date came from. When the list
    card carried it, the card has already vouched for the link and no further
    proof is asked, which is what keeps anthropic.com working: its genuine
    research posts declare `og:type: website`. When only the detail page knows
    its date, there is no such vouching, so the page has to say for itself that
    it is an article.
    """
    try:
        meta = page_meta(html_soup(http.get_text(url)))
    except Exception:  # noqa: BLE001 - one unreachable article must not end the site
        return None

    if list_date is None and not meta.declares_article:
        return None

    published = list_date or parse_date(meta.published_raw, assume_tz=assume_tz)
    if published is None or not meta.title:
        return None

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

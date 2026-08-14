"""Walk the site registry and gather everything published inside the window.

One rule governs this module: a site that fails must not take the run with it.
Thirty blogs on thirty stacks will always include one that is down, has moved
its feed, or has changed framework overnight. Each failure is recorded and
reported, and the other twenty-nine still arrive.

The registry's `fetcher` column decides which module handles a site, so adding
a site is a row rather than a branch.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable

from it_newsletter.fetchers import embedded, feed, html_list, sitemap
from it_newsletter.fetchers._common import Http
from it_newsletter.models import AppConfig, Article, Site, SiteResult
from it_newsletter.store import collapse_translations, dedupe
from it_newsletter.window import Window

logger = logging.getLogger(__name__)

FETCHERS: dict[str, Callable] = {
    "feed": feed.fetch,
    "embedded": embedded.fetch,
    "sitemap": sitemap.fetch,
    "html_list": html_list.fetch,
}


def collect_site(
    site: Site,
    window: Window,
    *,
    http: Http,
    known_urls: set[str] | None = None,
    max_pages: int = 1,
) -> SiteResult:
    """Collect one site, converting any failure into a recorded result."""
    fetcher = FETCHERS.get(site.fetcher)
    if fetcher is None:
        return SiteResult(site=site.name, error=f"unknown fetcher {site.fetcher!r}")

    kwargs = {"http": http}
    if site.fetcher in ("feed", "html_list", "sitemap"):
        kwargs["known_urls"] = known_urls or set()
    if site.fetcher == "html_list":
        kwargs["max_pages"] = max_pages

    articles: list[Article] = []
    newest: datetime | None = None
    errors: list[str] = []

    for target in source_variants(site):
        try:
            outcome = fetcher(target, window, **kwargs)
        except Exception as e:  # noqa: BLE001 - one site's failure is data, not a crash
            logger.warning("%s (%s): %s: %s",
                           site.name, target.source_url, type(e).__name__, e)
            errors.append(f"{type(e).__name__}: {e}")
            continue
        articles.extend(outcome.articles)
        if outcome.newest_seen and (newest is None or outcome.newest_seen > newest):
            newest = outcome.newest_seen

    # A site is only reported as failed when every one of its sources failed.
    # One dead category page should not hide the articles the others returned.
    if errors and not articles and newest is None:
        return SiteResult(site=site.name, error="; ".join(dict.fromkeys(errors)))

    articles = dedupe(articles)
    logger.info("%s: %d article(s)", site.name, len(articles))
    return SiteResult(
        site=site.name,
        articles=articles,
        newest_seen=newest,
        examined=len(articles),
    )


def source_variants(site: Site) -> list[Site]:
    """Every page of this blog worth reading, as sites in their own right.

    A blog stays one row in the registry even when its articles are spread over
    several pages, because the row's identity describes the blog, not a
    section of it. `params.extra_sources` names the rest: the full archive
    behind a highlights-only front page, or the category pages of a site with
    no combined listing.

    An entry is either a URL, or an object carrying its own parameters. The
    second form is not a nicety. Sections of one site routinely use different
    URL shapes, so anthropic.com needs `/research/[^/]+` for one source and
    `/news/[^/]+` for the other, and a single site-wide pattern would silently
    collect nothing from whichever section it did not match.

        extra_sources: ["https://example.com/archive"]
        extra_sources: [{"url": "https://example.com/news",
                         "article_regex": "/news/[^/]+"}]
    """
    entries = site.params.get("extra_sources") or []
    if isinstance(entries, (str, dict)):
        entries = [entries]

    variants = [site]
    seen = {site.source_url}
    for entry in entries:
        overrides = {"url": entry} if isinstance(entry, str) else dict(entry)
        url = overrides.pop("url", None)
        if not url or url in seen:
            continue
        seen.add(url)
        params = {**site.params, **overrides}
        params.pop("extra_sources", None)
        variants.append(site.model_copy(update={"source_url": url, "params": params}))
    return variants


def collect(
    config: AppConfig,
    window: Window,
    *,
    sites: list[Site] | None = None,
    known_urls: set[str] | None = None,
    max_pages: int | None = None,
) -> tuple[list[Article], list[SiteResult]]:
    """Collect every given site (active ones by default) and merge the results.

    Returns the deduplicated articles and the per-site outcomes. The outcomes
    are not a debugging aid: `state` folds them into what it knows about each
    site, and the email footer uses them to surface a parser that broke.
    """
    collection = config.settings.collection
    http = Http(
        timeout=collection.request_timeout,
        delay=collection.delay_between_requests,
    )
    targets = sites if sites is not None else config.enabled_sites()
    # A ceiling, not a target. Paging stops as soon as a list page reaches back
    # past the window, so a daily run reads one page from most sites and more
    # only from the ones that published enough to fill it.
    pages = max_pages if max_pages is not None else collection.max_list_pages

    results = [
        collect_site(site, window, http=http, known_urls=known_urls, max_pages=pages)
        for site in targets
    ]
    articles = collapse_translations(
        dedupe([a for result in results for a in result.articles]),
        language=config.settings.output.language,
    )

    failed = [r.site for r in results if not r.ok]
    logger.info(
        "collected %d article(s) from %d site(s); %d failed%s",
        len(articles), len(targets), len(failed),
        f" ({', '.join(failed)})" if failed else "",
    )
    return articles, results

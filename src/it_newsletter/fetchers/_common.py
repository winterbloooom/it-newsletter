"""The shared toolbox every fetcher draws from.

What lives here is whatever is the same no matter which site is being read:
the HTTP session, date parsing, feed normalization, and the handful of ways a
page exposes its own metadata. Site-specific knowledge stays in the registry
row, or failing that in the fetcher that needs it, but never here.

The date parser is the piece that earns this module. Every format below was
observed in the registry, and a site whose date silently fails to parse looks
identical to a site that published nothing, so `parse_date` returns None on
failure and callers are expected to treat that as a defect rather than a zero.
"""

from __future__ import annotations

import gzip
import html as html_module
import json
import re
import time
import urllib.request
import xml.etree.ElementTree as ET
import zlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

# Several sites in the registry refuse a non-browser User-Agent outright, and
# Medium answers one with an HTML page instead of the feed.
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# A complete browser header set, not just a User-Agent. Several sites reject a
# partial request outright and serve the same page happily once the rest is
# present: uber.com answered 406, perplexity.ai and ridicorp.com 403, all three
# on `Accept: */*` with nothing else. They were turning away an incomplete
# request rather than blocking this client, and a browser or a feed reader
# sends every one of these on an ordinary page load.
BROWSER_HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
              "application/rss+xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

_CHARSET_RE = re.compile(r"charset=([\w\-]+)", re.I)

# Path segments that mark navigation rather than an article. Used both when
# inferring a URL pattern and when applying one, because an inferred pattern is
# a shape and shapes catch author pages and tag indexes: amazon.science yielded
# entries titled "Byron Cook", shopify.engineering "Cody Mazza-Anthony author &
# reviewer at Shopify".
NAV_SEGMENTS = frozenset({
    "tag", "tags", "category", "categories", "author", "authors", "people",
    "page", "search", "about", "archive", "archives", "series", "topics",
    "topic", "login", "signup", "contact", "privacy", "terms", "rss", "feed",
    "newsletter", "events", "event", "careers", "jobs", "pricing", "docs",
})


# A leading path segment that names a language or country: `ko`, `kr`, `en-US`.
_LOCALE_SEGMENT = re.compile(r"^[a-z]{2}(?:[-_][A-Za-z]{2,4})?$")
MAX_LOCALE_PREFIXES = 2


def locale_variants(path: str) -> list[str]:
    """The path, plus the same path with its leading locale prefixes removed.

    Sites localize by IP as well as by header, so the same blog answers
    `/kr/ko/blog/<slug>` to a request from Seoul and something else to one from
    a US datacenter. A pattern inferred in one place then matches nothing in
    the other, which is how uber.com came to carry `^/kr/ko/blog/[^/]+/?$` in
    the registry and collect zero articles in CI.

    Matching every variant lets a registry pattern be written locale-free and
    still work wherever the run happens.
    """
    variants = [path]
    segments = [s for s in path.split("/") if s]
    for _ in range(MAX_LOCALE_PREFIXES):
        if not segments or not _LOCALE_SEGMENT.match(segments[0]):
            break
        segments = segments[1:]
        variants.append("/" + "/".join(segments))
    return variants


class Http:
    """A small HTTP client over `urllib.request`.

    Deliberately not `requests`. techblog.woowahan.com answers a `requests`
    call with a 403 challenge page no matter which headers are sent, while the
    identical request through urllib returns the feed: the WAF is fingerprinting
    the client, not reading the headers. The standard library reaches more of
    the registry and costs no dependency.

    `delay` is a courtesy pause between calls, applied per instance.
    """

    def __init__(self, *, timeout: int = 30, delay: float = 0.0) -> None:
        self.timeout = timeout
        self.delay = delay
        self._last_call = 0.0

    def get_bytes(self, url: str) -> bytes:
        if self.delay:
            elapsed = time.monotonic() - self._last_call
            if elapsed < self.delay:
                time.sleep(self.delay - elapsed)

        request = urllib.request.Request(url, headers=BROWSER_HEADERS)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
                encoding = (response.headers.get("Content-Encoding") or "").lower()
                self._content_type = response.headers.get("Content-Type") or ""
        finally:
            self._last_call = time.monotonic()

        if encoding == "gzip":
            raw = gzip.decompress(raw)
        elif encoding == "deflate":
            raw = zlib.decompress(raw, -zlib.MAX_WBITS)
        return raw

    def get_text(self, url: str) -> str:
        """Fetch and decode. Uses the declared charset, then falls back to UTF-8.

        Only for HTML. XML goes through `get_bytes`, because a feed's own
        declaration is more trustworthy than its HTTP header: tech.kakao.com
        serves `Content-Type: text/xml` with no charset, which by the HTTP
        specification means Latin-1, while the document says UTF-8. Decoding by
        the header turns every Korean title into mojibake.
        """
        raw = self.get_bytes(url)
        match = _CHARSET_RE.search(getattr(self, "_content_type", ""))
        if match:
            try:
                return raw.decode(match.group(1), "replace")
            except LookupError:
                pass
        return raw.decode("utf-8", "replace")


# XML 1.0 forbids most C0 control characters, but real feeds contain them:
# Medium's feeds carry a raw 0x08 or 0x1f inside article text, which makes the
# whole document unparseable. Removing them is the smallest repair that keeps
# the other ten articles readable.
_ILLEGAL_XML = re.compile(rb"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def sanitize_xml(raw: bytes) -> bytes:
    return _ILLEGAL_XML.sub(b"", raw)


def resolve_tz(name: str | None) -> tzinfo:
    return ZoneInfo(name) if name else timezone.utc


# Tracking parameters that vary per delivery channel. Medium appends
# `?source=rss----<hash>---4` to every feed link, so the same article arrives
# with a different URL depending on where it was read, and a URL used as a
# deduplication key has to have them removed.
_TRACKING_PARAMS = ("source", "ref", "fbclid", "gclid")


def canonical_url(url: str) -> str:
    """Strip tracking parameters and the fragment, so one article has one key."""
    parts = urlsplit(url)
    kept = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key not in _TRACKING_PARAMS and not key.startswith("utm_")
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), ""))


# ── Dates ──────────────────────────────────────────────────────

_NUMERIC = re.compile(r"(\d{4})\s*[.\-/]\s*(\d{1,2})\s*[.\-/]\s*(\d{1,2})")
_KOREAN = re.compile(r"(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일")
_ENGLISH = re.compile(
    r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+"
    r"(\d{1,2})(?:st|nd|rd|th)?\s*,?\s*(\d{4})?\b",
    re.I,
)
_RELATIVE = re.compile(r"(\d+)\s*(분|시간|일|주|개월|년)\s*전")
_RELATIVE_UNIT = {
    "분": timedelta(minutes=1),
    "시간": timedelta(hours=1),
    "일": timedelta(days=1),
    "주": timedelta(weeks=1),
    "개월": timedelta(days=30),
    "년": timedelta(days=365),
}
_MONTHS = {
    m: i for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun",
         "jul", "aug", "sep", "oct", "nov", "dec"], start=1
    )
}


def parse_date(
    text: str | None,
    *,
    assume_tz: tzinfo = timezone.utc,
    now: datetime | None = None,
) -> datetime | None:
    """Parse any date form the registry produces. Returns None if none match.

    A value with no offset is resolved with `assume_tz`, the site's configured
    zone. Left to the machine's local zone instead, the same feed would read
    nine hours apart on a KST laptop and a UTC runner, which moves articles
    across the window boundary in both directions.

    Handled, in the order tried:
        ISO 8601        2026-08-13T13:00:00Z, 2026-08-13
        RFC 822         Wed, 13 Aug 2026 13:00:00 +0900
        numeric         2026.07.22, 2026. 6. 12, 2026/8/13
        Korean          2026년 7월 29일
        English         July 29, 2026, Aug 10, 2026, Jul 6
        relative        5일 전, 21분 전
    """
    if not text:
        return None
    text = text.strip()
    if not text:
        return None

    parsed: datetime | None = None

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        pass

    if parsed is None and "," in text:
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            pass

    if parsed is None:
        match = _NUMERIC.search(text) or _KOREAN.search(text)
        if match:
            year, month, day = (int(g) for g in match.groups())
            try:
                parsed = datetime(year, month, day)
            except ValueError:
                parsed = None

    if parsed is None:
        parsed = _parse_english(text, assume_tz=assume_tz, now=now)

    if parsed is None:
        parsed = _parse_relative(text, assume_tz=assume_tz, now=now)

    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=assume_tz)
    return parsed


def _parse_english(
    text: str, *, assume_tz: tzinfo, now: datetime | None
) -> datetime | None:
    match = _ENGLISH.search(text)
    if not match:
        return None
    month = _MONTHS[match.group(1).lower()[:3]]
    day = int(match.group(2))
    if match.group(3):
        try:
            return datetime(int(match.group(3)), month, day)
        except ValueError:
            return None

    # No year, as on list cards that show only "Aug 10". Assume the current
    # year, then step back one if that lands in the future, which is what
    # happens across the new-year boundary.
    reference = (now or datetime.now(assume_tz)).astimezone(assume_tz)
    try:
        candidate = datetime(reference.year, month, day, tzinfo=assume_tz)
    except ValueError:
        return None
    if candidate > reference + timedelta(days=1):
        candidate = candidate.replace(year=reference.year - 1)
    return candidate


def _parse_relative(
    text: str, *, assume_tz: tzinfo, now: datetime | None
) -> datetime | None:
    match = _RELATIVE.search(text)
    if not match:
        return None
    reference = (now or datetime.now(assume_tz)).astimezone(assume_tz)
    return reference - int(match.group(1)) * _RELATIVE_UNIT[match.group(2)]


# ── Feeds ──────────────────────────────────────────────────────

ATOM_NS = "{http://www.w3.org/2005/Atom}"
CONTENT_NS = "{http://purl.org/rss/1.0/modules/content/}"
DC_NS = "{http://purl.org/dc/elements/1.1/}"


@dataclass(frozen=True)
class FeedItem:
    """RSS 2.0 `<item>`, Atom `<entry>`, and JSON Feed items, normalized."""

    title: str
    link: str
    summary: str
    author: str | None
    published_raw: str


def _rss_item(element: ET.Element) -> FeedItem:
    body = (
        element.findtext(f"{CONTENT_NS}encoded")
        or element.findtext("description")
        or ""
    )
    author = element.findtext("author") or element.findtext(f"{DC_NS}creator")
    return FeedItem(
        title=(element.findtext("title") or "").strip(),
        link=(element.findtext("link") or "").strip(),
        summary=body,
        author=author.strip() if author and author.strip() else None,
        published_raw=(
            element.findtext("pubDate") or element.findtext(f"{DC_NS}date") or ""
        ).strip(),
    )


def _atom_link(entry: ET.Element) -> str:
    fallback = ""
    for link in entry.findall(f"{ATOM_NS}link"):
        href = link.attrib.get("href", "")
        if not href:
            continue
        fallback = fallback or href
        if link.attrib.get("rel", "alternate") == "alternate":
            return href
    return fallback


def _atom_item(entry: ET.Element) -> FeedItem:
    author_el = entry.find(f"{ATOM_NS}author")
    author = author_el.findtext(f"{ATOM_NS}name") if author_el is not None else None
    return FeedItem(
        title=(entry.findtext(f"{ATOM_NS}title") or "").strip(),
        link=_atom_link(entry),
        summary=(
            entry.findtext(f"{ATOM_NS}summary")
            or entry.findtext(f"{ATOM_NS}content")
            or ""
        ),
        author=author.strip() if author and author.strip() else None,
        published_raw=(
            entry.findtext(f"{ATOM_NS}published")
            or entry.findtext(f"{ATOM_NS}updated")
            or ""
        ).strip(),
    )


def _json_feed_items(payload: dict) -> list[FeedItem]:
    items = []
    for item in payload.get("items", []):
        author = item.get("author") or {}
        authors = item.get("authors") or []
        name = author.get("name") if isinstance(author, dict) else None
        if not name and authors:
            name = authors[0].get("name")
        items.append(FeedItem(
            title=(item.get("title") or "").strip(),
            link=(item.get("url") or item.get("external_url") or "").strip(),
            summary=item.get("summary") or item.get("content_text") or item.get("content_html") or "",
            author=name,
            published_raw=(item.get("date_published") or item.get("date_modified") or "").strip(),
        ))
    return items


def feed_items(raw: bytes) -> list[FeedItem]:
    """Parse RSS 2.0, RSS 1.0, Atom, or JSON Feed into one shape.

    Takes bytes rather than text on purpose: an XML document declares its own
    encoding, and that declaration is more reliable than the HTTP header, which
    several sites in the registry either omit or get wrong.

    Raises on a document that is none of the four, so a site serving an HTML
    error page in place of its feed fails loudly instead of reporting zero
    articles. Medium does exactly this when the path is wrong.
    """
    stripped = raw.lstrip()
    if stripped.startswith(b"{"):
        return _json_feed_items(json.loads(stripped.decode("utf-8", "replace")))

    root = ET.fromstring(sanitize_xml(stripped))
    tag = root.tag.split("}")[-1]
    if tag == "rss":
        return [_rss_item(item) for item in root.findall(".//channel/item")]
    if tag == "feed":
        return [_atom_item(entry) for entry in root.findall(f"{ATOM_NS}entry")]
    if tag == "RDF":  # RSS 1.0
        rdf_ns = "{http://purl.org/rss/1.0/}"
        return [_rss_item(item) for item in root.findall(f"{rdf_ns}item")]
    raise ValueError(f"not a feed: root element is <{tag}>")


# ── HTML ───────────────────────────────────────────────────────


def html_soup(markup: str, *, unescape: bool = False) -> BeautifulSoup:
    if unescape:
        markup = html_module.unescape(markup)
    return BeautifulSoup(markup, "html.parser")


def html_to_text(node, *, unescape: bool = False, limit: int | None = None) -> str:
    """Line-preserving text from an HTML fragment or a parsed node."""
    if node is None:
        return ""
    if isinstance(node, str):
        node = html_soup(node, unescape=unescape)
    text = node.get_text("\n", strip=True)
    return text[:limit] if limit else text


# Elements that carry chrome rather than content. Removing them first is what
# lets the "biggest block of text wins" heuristic below pick the article
# instead of a navigation menu or a footer full of links.
_CHROME_TAGS = ("script", "style", "nav", "header", "footer", "aside", "form", "noscript")

# Containers an article is likely to sit in, most specific first.
_BODY_SELECTORS = (
    "article", "main", "[role=main]", ".post-content", ".article-content",
    ".entry-content", ".post-body", ".prose", "#content", ".content",
)


def main_text(soup: BeautifulSoup, *, limit: int = 6000) -> str:
    """The article's body text, without site chrome.

    Used only for the handful of articles that get summarized, so it favours
    working everywhere over being exact anywhere. Candidate containers are
    tried in order and the one holding the most text wins, which beats trusting
    a single selector across thirty different site templates.
    """
    for tag in soup(_CHROME_TAGS):
        tag.decompose()

    best = ""
    for selector in _BODY_SELECTORS:
        for element in soup.select(selector):
            text = element.get_text("\n", strip=True)
            if len(text) > len(best):
                best = text
    if not best:
        body = soup.find("body")
        best = body.get_text("\n", strip=True) if body else soup.get_text("\n", strip=True)

    # Collapse the blank-line runs that decomposing chrome leaves behind.
    return re.sub(r"\n{3,}", "\n\n", best)[:limit]


def meta_content(soup: BeautifulSoup, key: str) -> str | None:
    """`<meta property=...>` or `<meta name=...>` content. og:title and friends."""
    element = soup.find("meta", property=key) or soup.find("meta", attrs={"name": key})
    content = element.get("content") if element else None
    return content.strip() if content else None


_ARTICLE_TYPES = {"Article", "BlogPosting", "NewsArticle", "TechArticle", "Report"}


def jsonld_article(soup: BeautifulSoup) -> dict | None:
    """The page's first article-shaped JSON-LD block, if it has one."""
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (ValueError, TypeError):
            continue
        for obj in _walk_jsonld(data):
            types = obj.get("@type")
            types = types if isinstance(types, list) else [types]
            if any(t in _ARTICLE_TYPES for t in types):
                return obj
    return None


def _walk_jsonld(data) -> list[dict]:
    """Flatten a JSON-LD document, including @graph containers."""
    out: list[dict] = []
    stack = [data]
    while stack:
        current = stack.pop()
        if isinstance(current, list):
            stack.extend(current)
        elif isinstance(current, dict):
            out.append(current)
            if "@graph" in current:
                stack.append(current["@graph"])
    return out


def jsonld_published(soup: BeautifulSoup) -> str | None:
    """A `datePublished` from any JSON-LD node on the page.

    Deliberately not restricted to article-typed nodes, unlike
    `jsonld_article`. sendbird.com dates its posts on a `WebPage` node, and
    every page is a WebPage, so requiring the article type there loses the date
    entirely. Whether the page *is* an article is a separate question with its
    own answer below.
    """
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (ValueError, TypeError):
            continue
        for obj in _walk_jsonld(data):
            published = obj.get("datePublished")
            if isinstance(published, str) and published.strip():
                return published.strip()
    return None


def _jsonld_author(obj: dict) -> str | None:
    author = obj.get("author")
    if isinstance(author, dict):
        return author.get("name")
    if isinstance(author, list) and author:
        first = author[0]
        return first.get("name") if isinstance(first, dict) else str(first)
    return author if isinstance(author, str) else None


@dataclass(frozen=True)
class PageMeta:
    """What a detail page exposes about itself, by whatever means it exposes it."""

    title: str | None
    published_raw: str | None
    author: str | None
    subtitle: str | None
    og_type: str | None = None
    has_article_schema: bool = False

    @property
    def declares_article(self) -> bool:
        """Whether the page says, in some standard way, that it is an article.

        Used to tell a real post from a category or product page that happened
        to match a URL pattern. It is evidence of an article, never evidence
        against one: anthropic.com serves genuine research posts with
        `og:type: website`, so absence here means "unknown", not "not an
        article", and callers weigh it accordingly.
        """
        return self.og_type == "article" or self.has_article_schema


def page_meta(soup: BeautifulSoup) -> PageMeta:
    """Read a detail page's own metadata, trying each source in turn.

    The `<time>` fallback is not optional. 카카오페이 publishes no
    `article:published_time` and no JSON-LD; its only date is the text of a
    `<time>` element, and its subtitle exists only as `og:description`.
    """
    article = jsonld_article(soup)

    title = meta_content(soup, "og:title")
    if not title and article:
        title = article.get("headline")
    if not title:
        h1 = soup.find("h1")
        title = h1.get_text(" ", strip=True) if h1 else None

    published = meta_content(soup, "article:published_time")
    if not published and article:
        published = article.get("datePublished")
    jsonld_date = None
    if not published:
        jsonld_date = jsonld_published(soup)
        published = jsonld_date
    if not published:
        time_el = soup.find("time")
        if time_el is not None:
            published = time_el.get("datetime") or time_el.get_text(" ", strip=True)

    author = _jsonld_author(article) if article else None
    if not author:
        author = meta_content(soup, "author") or meta_content(soup, "article:author")

    subtitle = meta_content(soup, "og:description")
    if not subtitle and article:
        subtitle = article.get("description")

    return PageMeta(
        title=title.strip() if title else None,
        published_raw=published.strip() if published else None,
        author=author.strip() if author else None,
        subtitle=subtitle.strip() if subtitle else None,
        og_type=meta_content(soup, "og:type"),
        # A JSON-LD `datePublished` counts as an article declaration. Measured
        # against the registry it does not over-fire: figma.com's category
        # pages and cohere.com's product pages carry no such node, while
        # sendbird.com's posts carry nothing else.
        has_article_schema=article is not None
        or meta_content(soup, "article:published_time") is not None
        or jsonld_date is not None,
    )

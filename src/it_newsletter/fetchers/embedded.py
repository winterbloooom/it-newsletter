"""Tier 2: the article list is already in the page, as JSON.

A JavaScript-rendered site usually ships its own data alongside the markup so
the client can hydrate. That data has real field names and real timestamps,
which makes it a better source than the rendered HTML, and it is why this tier
outranks scraping.

The container is generic, the field names are not: clova.ai keeps posts under
`props.pageProps.posts.edges[].node` with a `date`, while engineering.clova.ai
keeps them under `props.pageProps.initialRecentPostsResponse.posts[]` with a
`createdAt`. So the walker finds any list of records that carries both a title
and a date, unwraps GraphQL `{node: ...}` envelopes, and maps field names
through a table of aliases. Only the URL usually needs a parameter, because
many records store a slug rather than a link.

params:
    source        "next_data" (a __NEXT_DATA__ script), "rsc" (an app-router
                  streaming payload), or "js_literal" (a plain array assigned
                  in a script tag). `discover.py` reports which.
    variable      (js_literal only) the name assigned, e.g. "POSTS".
    url_template  (optional) builds the article URL from record fields, e.g.
                  "https://clova.ai/tech-blog/{slug}". Without it the record
                  must carry a url, link, permalink or path field.
    field_map     (optional) overrides the alias table for sites whose field
                  names carry a suffix the aliases cannot guess, such as
                  krafton-ai's bilingual title_kr / title_en pairs.
    path          (optional) dotted path to the record list, when the walker
                  picks the wrong one.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import quote, urljoin

from it_newsletter.fetchers._common import (
    Http,
    canonical_url,
    html_to_text,
    parse_date,
    resolve_tz,
)
from it_newsletter.models import Article, FetchOutcome, Site
from it_newsletter.window import Window

TITLE_KEYS = ("title", "headline", "name")
DATE_KEYS = (
    "date", "createdAt", "created_at", "publishedAt", "published_at",
    "datePublished", "publishDate", "published", "pubDate", "first_published_at",
)
URL_KEYS = ("url", "link", "permalink", "href", "path", "slug")
SUBTITLE_KEYS = ("excerpt", "description", "summary", "subtitle", "dek", "preview")
AUTHOR_KEYS = ("author", "authors", "byline", "writer", "creator")

SUBTITLE_LIMIT = 300


def fetch(
    site: Site,
    window: Window,
    *,
    http: Http,
) -> FetchOutcome:
    """Collect the site's articles inside the window.

    Raises when no record list can be found, because an empty result here means
    the page stopped shipping its data, not that nobody published.
    """
    html = http.get_text(site.source_url)
    source = site.params.get("source", "next_data")
    payload = _extract_payload(html, source, site.params)
    if payload is None:
        raise ValueError(
            f"no {source} payload found on {site.source_url}; "
            f"re-run discovery, the page may have changed framework"
        )

    keys = _keys_for(site.params.get("field_map") or {})
    records = _records_at(payload, site.params["path"], keys) if site.params.get("path") \
        else _find_records(payload, keys)
    if not records:
        raise ValueError(
            f"{source} payload on {site.source_url} contains no list of records "
            f"with both a title and a date; set params.path to point at it"
        )

    assume_tz = resolve_tz(site.tz)
    template = site.params.get("url_template")
    articles: list[Article] = []
    newest_seen: datetime | None = None

    for record in records:
        title = _first(record, keys.title)
        published = parse_date(_first(record, keys.date), assume_tz=assume_tz)
        url = _record_url(record, template=template, base=site.source_url, keys=keys)
        if not title or published is None or not url:
            continue
        if newest_seen is None or published > newest_seen:
            newest_seen = published
        if not window.contains(published):
            continue
        subtitle = _first(record, keys.subtitle)
        articles.append(Article(
            site=site.name,
            title=str(title).strip(),
            url=canonical_url(url),
            published_at=published,
            author=_author(record, keys),
            subtitle=html_to_text(str(subtitle), limit=SUBTITLE_LIMIT) if subtitle else None,
        ))
    return FetchOutcome(articles=articles, newest_seen=newest_seen)


@dataclass(frozen=True)
class _Keys:
    """The field names to read, after any `field_map` override is applied."""

    title: tuple[str, ...] = TITLE_KEYS
    date: tuple[str, ...] = DATE_KEYS
    url: tuple[str, ...] = URL_KEYS
    subtitle: tuple[str, ...] = SUBTITLE_KEYS
    author: tuple[str, ...] = AUTHOR_KEYS


def _keys_for(field_map: dict[str, str]) -> _Keys:
    """A mapped field wins outright; unmapped ones keep the alias list."""
    def pick(role: str, default: tuple[str, ...]) -> tuple[str, ...]:
        override = field_map.get(role)
        return (override,) if override else default

    return _Keys(
        title=pick("title", TITLE_KEYS),
        date=pick("date", DATE_KEYS),
        url=pick("url", URL_KEYS),
        subtitle=pick("subtitle", SUBTITLE_KEYS),
        author=pick("author", AUTHOR_KEYS),
    )


def _extract_payload(html: str, source: str, params: dict) -> Any | None:
    if source == "next_data":
        return _next_data(html)
    if source == "rsc":
        return _rsc_payload(html)
    if source == "js_literal":
        return _js_literal(html, params.get("variable", "POSTS"))
    raise ValueError(f"unknown embedded source {source!r}")


# ── payload extraction ─────────────────────────────────────────


def _next_data(html: str) -> Any | None:
    match = re.search(r'id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return None


def _rsc_payload(html: str) -> Any | None:
    """Reassemble a Next.js app-router streaming payload.

    The page emits `self.__next_f.push([1,"<chunk>"])` calls whose string
    arguments concatenate into one React Flight document. That document is not
    valid JSON as a whole, so rather than parse it we pull out the balanced
    JSON arrays and objects it contains and hand them to the same walker.
    """
    chunks = re.findall(r'self\.__next_f\.push\(\[1,\s*("(?:[^"\\]|\\.)*")\s*\]\)', html)
    if not chunks:
        return None
    try:
        flight = "".join(json.loads(chunk) for chunk in chunks)
    except json.JSONDecodeError:
        return None

    payloads: list[Any] = []
    decoder = json.JSONDecoder()
    for index, character in enumerate(flight):
        if character not in "[{":
            continue
        try:
            value, _ = decoder.raw_decode(flight, index)
        except ValueError:
            continue
        if isinstance(value, (list, dict)):
            payloads.append(value)
    return payloads or None


def _js_literal(html: str, variable: str) -> Any | None:
    """Read an array assigned in a script tag, written as a JavaScript literal.

    krafton-ai's blog is a single page holding `const POSTS = [...]`, with
    unquoted keys and non-breaking spaces for indentation, so it is JavaScript
    rather than JSON. Converting is cheaper and far more stable than scraping
    the rendered page, which builds its markup from this same array.
    """
    match = re.search(rf"\b(?:const|let|var)\s+{re.escape(variable)}\s*=\s*\[", html)
    if not match:
        return None

    start = html.index("[", match.end() - 1)
    body = _balanced(html, start)
    if body is None:
        return None
    try:
        return json.loads(_js_to_json(body))
    except json.JSONDecodeError:
        return None


def _balanced(text: str, start: int) -> str | None:
    """The bracketed span beginning at `start`, ignoring brackets inside strings."""
    opening = text[start]
    closing = {"[": "]", "{": "}"}[opening]
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                return text[start: index + 1]
    return None


_UNQUOTED_KEY = re.compile(r"([{,]\s*)([A-Za-z_$][\w$]*)(\s*:)")
_TRAILING_COMMA = re.compile(r",(\s*[}\]])")
_COMMENT = re.compile(r"//[^\n]*|/\*.*?\*/", re.S)


def _js_to_json(source: str) -> str:
    """Quote bare keys, strip comments, drop trailing commas.

    Only the text between string literals is rewritten. That separation is what
    makes comment removal safe: krafton-ai's array ends with a
    `// ← 새 글은 여기에 추가!` note, while its records hold URLs whose `https://`
    would be destroyed by a comment regex applied to the whole document.
    """
    source = source.replace("\xa0", " ")
    pieces: list[str] = []
    index = 0
    for match in re.finditer(r'"(?:[^"\\]|\\.)*"', source):
        pieces.append(_clean_outside_strings(source[index:match.start()]))
        pieces.append(match.group(0))
        index = match.end()
    pieces.append(_clean_outside_strings(source[index:]))
    return _TRAILING_COMMA.sub(r"\1", "".join(pieces))


def _clean_outside_strings(segment: str) -> str:
    return _UNQUOTED_KEY.sub(r'\1"\2"\3', _COMMENT.sub("", segment))


# ── record discovery ───────────────────────────────────────────


def _unwrap(item: Any) -> Any:
    """GraphQL responses wrap each row as {"node": {...}}. Look through it."""
    if isinstance(item, dict) and set(item) == {"node"} and isinstance(item["node"], dict):
        return item["node"]
    return item


def _looks_like_records(value: Any, keys: "_Keys") -> list[dict] | None:
    if not isinstance(value, list) or len(value) < 2:
        return None
    rows = [_unwrap(item) for item in value]
    if not all(isinstance(row, dict) for row in rows):
        return None
    sample = rows[:3]
    has_title = all(_first(row, keys.title) for row in sample)
    has_date = all(_first(row, keys.date) for row in sample)
    return rows if has_title and has_date else None


# Total nodes the walk may visit. A budget, rather than a per-list cap: an RSC
# payload arrives as hundreds of separately-decoded fragments, and truncating
# each list to its first 50 entries meant blog-tech.tadatada.com's `articles`
# array, sitting past that mark in a list of 387, was never looked at.
MAX_NODES = 50_000
MAX_DEPTH = 14


def _find_records(payload: Any, keys: "_Keys") -> list[dict]:
    """The largest list of article-shaped records anywhere in the payload.

    Largest, not first: a page often carries a short "featured" list next to
    the full one, and the full one is what a daily window should be applied to.
    """
    best: list[dict] = []
    stack: list[tuple[Any, int]] = [(payload, 0)]
    visited = 0
    while stack and visited < MAX_NODES:
        current, level = stack.pop()
        visited += 1
        if level > MAX_DEPTH:
            continue
        found = _looks_like_records(current, keys)
        if found and len(found) > len(best):
            best = found
        if isinstance(current, dict):
            stack.extend((value, level + 1) for value in current.values())
        elif isinstance(current, list):
            stack.extend((item, level + 1) for item in current)
    return best


def _records_at(payload: Any, path: str, keys: "_Keys") -> list[dict]:
    """Follow a dotted path, for when the walker's choice needs overriding."""
    current = payload
    for part in path.split("."):
        if not part:
            continue
        if isinstance(current, list):
            current = current[int(part)]
        else:
            current = current.get(part)
        if current is None:
            return []
    return _looks_like_records(current, keys) or []


# ── field mapping ──────────────────────────────────────────────


def _first(record: dict, keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value
        if value not in (None, "", [], {}):
            return value
    return None


def _record_url(record: dict, *, template: str | None, base: str, keys: "_Keys") -> str | None:
    if template:
        try:
            return template.format(**{
                key: quote(str(value), safe="") if key == "slug" else value
                for key, value in record.items()
                if not isinstance(value, (dict, list))
            })
        except KeyError:
            return None
    for key in keys.url:
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return urljoin(base, value)
    return None


def _author(record: dict, keys: "_Keys") -> str | None:
    value = _first(record, keys.author)
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        name = value.get("name") or value.get("node", {}).get("name")
        return str(name).strip() if name else None
    if isinstance(value, list):
        names = []
        for entry in value:
            if isinstance(entry, dict) and entry.get("name"):
                names.append(str(entry["name"]))
            elif isinstance(entry, str):
                names.append(entry)
        return ", ".join(names) or None
    return None

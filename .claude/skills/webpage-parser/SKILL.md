---
name: webpage-parser
description: How to make a site's article list machine-readable for this project — the four tiers, from standard feed down to HTML scraping, what counts as success, and the minimum fields a parser must produce. Consult when adding a site to config/sites.csv, when a site suddenly returns zero articles, or when writing or changing anything under src/it_newsletter/fetchers/.
---

# Webpage parser

Every site in the registry has to yield the same five fields. Three are mandatory
and two are conditional:

| Field | Required | Notes |
|---|---|---|
| `published_at` | yes | Absolute instant. A date with no timezone is resolved with the site's `tz`, never the runner's local zone. |
| `title` | yes | |
| `url` | yes | Absolute, canonical. This is the deduplication key. |
| `author` | when the site exposes one | Many company blogs do not. |
| `subtitle` | when the site exposes one | Feed description, `og:description`, or a dek element. |

A site that cannot produce the three mandatory fields does not go in the registry.
A site that merely lacks `author` and `subtitle` is normal and fine.

## The three rules

**Use what exists.** Sites publish their own article lists. Reach for a feed
before reading any HTML, and reach for the page's own embedded JSON before
writing a CSS selector. Scraping is what you do when the site offers nothing.

**When nothing exists, implement the least that works.** Prefer adding a row of
parameters to `config/sites.csv` over adding a branch to a fetcher, and adding a
branch over adding a file. Code is the last resort, and one site's quirk never
becomes a new module.

**One failure is not an answer.** Every tier below has several probes. Work
through all of them before declaring the tier dead, and through all four tiers
before declaring the site unparseable. Most apparent dead ends in this project
were one untried path away from a clean feed.

## The four tiers

Start at tier 1. Stop at the first tier that yields entries.

### Tier 1: a standard feed

Try, in order:

1. **Declared.** Fetch the page, take every
   `<link rel="alternate" type="application/rss+xml|atom+xml|feed+json">` href.
2. **Guessed.** Append each of `feed`, `rss`, `rss.xml`, `feed.xml`, `atom.xml`,
   `index.xml`, `rss/`, `feed/` to the site URL, and to its origin root.
3. **Host-specific forms** that no generic guess finds:

   | Host shape | Feed |
   |---|---|
   | `medium.com/<pub>` | `medium.com/feed/<pub>` — note the order |
   | Medium on a custom domain | `<domain>/feed` |
   | Hugo | `<section>/index.xml`, e.g. `devblog.croquis.com/ko/index.xml` |
   | `d2.naver.com` | `d2.naver.com/d2.atom` |
   | Tistory | `<domain>/rss` |
   | OpenAI | `openai.com/news/rss.xml` covers the research posts too |

The Medium case is the reason rule three exists. `medium.com/daangn/feed` returns
HTTP 200 with a 90 KB HTML page, so anything that checks only the status code
concludes there is no feed. Six blogs were misfiled that way on the first pass.

### Tier 2: JSON embedded in the page

The list is already in the HTML as data. Search the page source for:

- `<script type="application/ld+json">` — `ItemList`, `Blog`, `BlogPosting`.
- `<script id="__NEXT_DATA__">` — Next.js pages router. The post list is under
  `props.pageProps`. NAVER CLOVA's tech blog is this shape.
- `self.__next_f.push(` — Next.js app router streaming its React Server Component
  payload. The chunks concatenate into a JSON-ish blob; the article records are in
  there. Anthropic's research index is this shape.
- `window.__NUXT__`, `window.__INITIAL_STATE__`, or a bare `const POSTS = [` for
  hand-rolled pages.

Prefer this tier over tier 3 whenever both are possible. Embedded JSON carries
real field names and real timestamps; HTML carries whatever the layout needed.

### Tier 3a: the sitemap

Before scraping a listing, check `/sitemap.xml`. It is the site's own
machine-readable list of its URLs, so nothing has to be inferred, and it often
carries `lastmod`. Follow one level of `<sitemapindex>`. figma.com names 799
posts there while its listing yields none.

Two cautions. `lastmod` is a modification time, not a publication time, so a
post edited today looks new; use it to decide which pages are worth opening,
not as the date. And a sitemap lists every page the site wants indexed, tag and
category pages included, so the article check below still applies.

### Tier 3b: list page HTML, then the detail page

Two halves, and you often need both.

**From the list page**, collect anchors matching a URL pattern, and the date text
nearest each anchor. Search the anchor's own text first, then walk up at most four
ancestors. Beyond that you start picking up a sidebar's dates.

**From the detail page**, fill whatever the list did not give, in this order:

| Field | Sources, in order |
|---|---|
| `title` | `og:title`, JSON-LD `headline`, `<h1>` |
| `published_at` | `article:published_time`, JSON-LD `datePublished`, `<time datetime>`, `<time>` text |
| `author` | JSON-LD `author.name`, `<meta name="author">`, `article:author` |
| `subtitle` | `og:description`, JSON-LD `description` |

`<time>` matters more than it looks. 카카오페이 publishes no
`article:published_time` and no JSON-LD; its only date is
`<time data-astro-cid-qlfjksao>2026. 6. 12</time>`. Its subtitle is only in
`og:description`.

Fetching the detail page costs one request per article, so filter by the
collection window on list-page dates first when the list has them.

### Tier 4: site-specific handling

Only after tiers 1 to 3 are genuinely exhausted. Keep it to a parameter or a
narrow branch, note in `config/sites.csv` why the site needs it, and prefer a
shape that would break loudly rather than silently if the site changes.

`krafton-ai.github.io/blog/` is the current example: a single page with its posts
hardcoded in an inline `const POSTS = [...]` array, with `date_en`, `title_kr`,
`excerpt_kr` and friends. No feed, no structured data, no per-article URLs.

## One blog, several pages

A front page is often not the whole blog. Three shapes, and they compose:

| Shape | What you see | What to set |
|---|---|---|
| Paginated list | `/page/2`, `?page=2`, a "next" link, `hasNextPage` in embedded JSON | `page_url` with a `{page}` placeholder |
| Highlights front page, real list elsewhere | a handful of featured posts, an "all posts" or "archive" link | point `source_url` at the full list |
| Sections with no combined list | `/research` and `/news`, or per-category pages | `extra_sources` |

Measured on this registry: tech.kakaopay.com shows **5 posts on a front page
that advertises 32 pages**. Before `page_url` was set, that site returned 2
articles for any window, however wide. With it, a 400-day window returns 18.

**Check the reach, not just the parse.** A source that returns articles can
still be returning a fraction of them. Ask the page how much it is hiding: look
for a pager, a total count, or a next-page flag, and compare the oldest article
you get against the oldest the blog actually has.

**Paging stops at the window, not at a page count.** `max_list_pages` is a
ceiling for runaway cases. The real stop condition is a list page reaching back
past the window start, which is why list pages must be read newest first and
why the check runs on the cards rather than on what survived the window filter.
A page holding only older posts would otherwise look empty rather than
finished, and paging would walk the site's entire history.

**Sections need their own parameters.** `extra_sources` entries may be a bare
URL, which inherits the site's parameters, or an object carrying overrides.
Anthropic needs the second form: `/research/[^/]+` and `/news/[^/]+` are
different shapes, and one site-wide pattern collects nothing from whichever
section it fails to match, silently. Adding the news section took that site
from 5 articles to 11 over three weeks.

## Matching a URL is not finding an article

An inferred `article_regex` is a shape, and shapes over-match. On
figma.com/blog, `/blog/[^/]+` catches `/blog/engineering/` and every other
category alongside the real posts, and each of those yields a page with a
title. The digest then fills with entries called "All Posts | Figma Blog".

**A page has to say it is an article, unless the list already vouched for it.**
The standard declarations are `og:type: article`, a JSON-LD `Article` or
`BlogPosting` block, and an `article:published_time` meta. Absence is not
proof of the opposite: anthropic.com serves genuine research posts with
`og:type: website`. So the check is conditional, and `date_from` decides it.

**`date_from` is a claim about where the truth lives, and it is load-bearing
twice over.** It picks the date, and it picks whether the page is trusted:

| `date_from` | Date comes from | Page must declare itself an article |
|---|---|---|
| `detail` | the article page | yes, since nothing else vouches for the URL |
| `list` | the list card | no, the card already did |

Prefer `detail` wherever it works. A date read from the article belongs to that
article, while a date read from a list page is whatever sat nearest the link.
Use `list` only for sites whose article pages genuinely do not know their own
date: engineering.ab180.co marks every post `og:type: article` and dates none
of them.

**Decide it by sampling article pages, not by counting dates near links.** The
first version of this test counted list cards with a date nearby and called
figma.com a `list` site, because its categories sit next to dates too. Fetching
two or three matched URLs and asking whether they carry a date answers the
actual question.

**A listing is rarely in the order you assume.** Stopping at the first article
older than the window assumes strictly newest-first, and real listings are not:
figma.com/blog/everything puts its category links first and mixes evergreen
posts among recent ones. Require a run of consecutive old articles before
declaring a page exhausted, and set `max_articles` high enough to clear the
navigation that sits at the top.

**A blocked request is often just an incomplete one.** uber.com answered 406,
perplexity.ai and ridicorp.com 403, all to a request carrying a User-Agent and
`Accept: */*` and nothing else. All three return 200 to the header set a
browser actually sends: `Accept` with real media types, `Accept-Language`,
`Accept-Encoding`, `Sec-Fetch-*`, `Upgrade-Insecure-Requests`. Send the whole
set before concluding a site is closed to you. ridicorp.com turned out to have
an ordinary WordPress feed behind the 403.

**A date is a date, whatever node declares it.** JSON-LD `datePublished` is
worth reading from any node, not only an `Article` one: sendbird.com dates its
posts on a `WebPage`, and every page is a WebPage, so restricting the search by
type lost the date entirely and the site looked unparseable. Whether the page
*is* an article stays a separate question with its own answer. That said, a
JSON-LD `datePublished` is itself decent evidence of an article, and it does
not over-fire: measured across the registry, figma.com's category pages and
cohere.com's product pages carry no such node.

**When only some of a site's sections render, take the ones that do.**
linkedin.com's blog index is client-side, and so are its `ai`,
`generative-ai` and `machine-learning` sections; 23 of its 30 category pages
are server-rendered and date their cards. Enumerate them once, keep the ones
that both render and match the reader's subjects, and let `extra_sources` plus
URL deduplication handle the overlap.

**Verify the extraction, not the marker.** The same mistake appears one tier
up: nearly every Next.js site ships a `__NEXT_DATA__` script or a streaming RSC
payload, and most keep no article list in it. Detecting the marker alone sent
eight of ten sites to the embedded fetcher, where all eight failed. Run the
record walk during discovery and accept the tier only when records come out.

## What counts as success

**Entries, not a 200.** A response can parse as valid RSS and contain nothing.
`tech.kakaopay.com/rss.xml` is exactly this: well-formed, zero `<item>`. A
detector that stops at "it parsed" marks that site as working and it silently
reports zero articles every day. Assert `len(entries) > 0`.

**A real date on a real article.** Confirm the newest entry's `published_at`
parses and is not the epoch. A date parser that silently returns 0.0 on failure
turns a broken site into an empty one.

**A URL that resolves.** Relative hrefs get joined against the list URL.

Verify against the newest few articles, not the first one you find. List pages
put pinned or featured posts at the top, and those have different markup.

## Dates

All of these appear in the current registry, so the shared parser in
`fetchers/_common.py` handles all of them. Add to that function; never write a
second date parser.

```plaintext
RFC 822      Wed, 13 Aug 2026 13:00:00 +0900     RSS pubDate
ISO 8601     2026-08-13T13:00:00Z                Atom, JSON-LD
dotted       2026.07.22                          카카오모빌리티
spaced       2026. 6. 12                         카카오페이
Korean       2026년 7월 29일                      KRAFTON
English      July 29, 2026  /  Aug 10, 2026      KRAFTON, Anthropic
relative     5일 전                               list cards with no absolute date
```

A date with no timezone is resolved with the site's configured `tz`. Left to
`.timestamp()`, the same feed reads nine hours apart on a KST laptop and a UTC
runner, which moves articles across the window boundary.

## When a live site goes quiet

Zero articles is ambiguous: a slow week, or a parser that broke. Before touching
anything, re-run discovery against the site. If the feed URL now 404s or returns
zero entries where it used to return ten, the site changed and you start
again from tier 1. `is_active` answers a different question (has this blog
posted in six months) and is not evidence about the parser.

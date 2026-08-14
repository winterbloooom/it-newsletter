"""Reading and writing the collected artifacts.

One JSONL file per run, one article per line. The format is deliberately dull:
a line is a small JSON object with empty fields omitted, so a year of daily
files stays in the low megabytes and any line can be read without the rest.

`data/` is a cache, not the source of truth. Nothing here is irreplaceable,
because a missing range is re-crawled rather than mourned. That is what lets
the daily run publish to an expiring CI artifact without risking the archive.

    data/
      daily/2026-08-13.jsonl        one per collection window, named by its end
      scan/2026-08-13-last365d.jsonl   ad-hoc range queries, kept apart
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path

from it_newsletter.config import REPO_ROOT
from it_newsletter.models import Article

DAILY_DIR = "daily"
SCAN_DIR = "scan"


def data_root(data_dir: str) -> Path:
    path = Path(data_dir)
    return path if path.is_absolute() else REPO_ROOT / path


def daily_path(data_dir: str, day: date) -> Path:
    return data_root(data_dir) / DAILY_DIR / f"{day.isoformat()}.jsonl"


def scan_path(data_dir: str, label: str) -> Path:
    return data_root(data_dir) / SCAN_DIR / f"{label}.jsonl"


def write(path: Path, articles: list[Article]) -> Path:
    """Write articles newest first, so the head of the file is the useful end."""
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(articles, key=lambda a: a.published_at, reverse=True)
    with path.open("w", encoding="utf-8") as f:
        for article in ordered:
            f.write(json.dumps(article.to_record(), ensure_ascii=False) + "\n")
    return path


def merge_write(path: Path, articles: list[Article]) -> Path:
    """Fold articles into whatever the file already holds, then write it back.

    A day's file is written by whichever run happens, and a run can happen more
    than once: a manual re-run, a retry after a delivery failure, or a
    `--only` invocation covering three sites. Overwriting would silently
    discard the earlier run's work, so the two sets are merged and deduplicated
    instead, keeping the richer copy of any article present in both.
    """
    return write(path, dedupe(read(path) + articles))


def read(path: Path) -> list[Article]:
    if not path.exists():
        return []
    articles = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                articles.append(Article(**json.loads(line)))
    return articles


def read_range(
    data_dir: str, start: datetime, end: datetime
) -> tuple[list[Article], list[date]]:
    """Everything already stored between two instants, and the days with no file.

    The missing days are the point: a range query fills those by crawling and
    leaves the rest alone.
    """
    root = data_root(data_dir) / DAILY_DIR
    articles: list[Article] = []
    missing: list[date] = []

    day = start.date()
    while day <= end.date():
        path = root / f"{day.isoformat()}.jsonl"
        if path.exists():
            articles.extend(a for a in read(path) if start <= a.published_at < end)
        else:
            missing.append(day)
        day += timedelta(days=1)
    return dedupe(articles), missing


def known_urls(data_dir: str, *, days: int = 30, today: date | None = None) -> set[str]:
    """URLs already stored recently.

    Fetchers use this to skip work they have already done. It is what keeps a
    site whose dates live on detail pages from re-requesting the same articles
    every morning.
    """
    root = data_root(data_dir) / DAILY_DIR
    if not root.exists():
        return set()
    cutoff = (today or date.today()) - timedelta(days=days)
    urls: set[str] = set()
    for path in root.glob("*.jsonl"):
        try:
            if date.fromisoformat(path.stem) < cutoff:
                continue
        except ValueError:
            continue
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    urls.add(json.loads(line)["url"])
    return urls


def dedupe(articles: list[Article]) -> list[Article]:
    """One article per URL, keeping the entry that carries the most.

    The same post reaches us twice when a range query overlaps a stored day, or
    when a site appears under two registry rows. Later passes may have a
    summary the earlier one lacked, so richness decides rather than order.
    """
    best: dict[str, Article] = {}
    for article in articles:
        existing = best.get(article.url)
        if existing is None or _richness(article) > _richness(existing):
            best[article.url] = article
    return sorted(best.values(), key=lambda a: a.published_at, reverse=True)


_HANGUL = re.compile(r"[가-힣]")

# Two posts from the same author on the same blog, this close together, are one
# article published twice rather than two articles. Wide enough to catch a
# publishing script that stamps them a minute apart, narrow enough that a blog
# posting twice in an afternoon is unaffected.
_TRANSLATION_WINDOW = timedelta(minutes=15)


def collapse_translations(articles: list[Article], *, language: str) -> list[Article]:
    """Keep one copy of an article that was published in two languages.

    Bilingual engineering blogs post the same piece twice, and both copies then
    compete for the same digest. 당근 publishes its Korean and English versions
    a minute apart under one author, which cost two of five summary slots on a
    real run.

    The pair is identified by blog, author and publication time rather than by
    comparing titles, because the titles are translations of each other and
    share no words. Whichever copy matches the configured output language wins.
    """
    by_author: dict[tuple[str, str], list[Article]] = {}
    kept: list[Article] = []
    for article in articles:
        if article.author:
            by_author.setdefault((article.site, article.author), []).append(article)
        else:
            # Without an author there is no evidence that two posts are one
            # article, so an unattributed post is never collapsed.
            kept.append(article)

    for group in by_author.values():
        group.sort(key=lambda a: a.published_at)
        cluster = [group[0]]
        for article in group[1:]:
            if article.published_at - cluster[-1].published_at <= _TRANSLATION_WINDOW:
                cluster.append(article)
            else:
                kept.append(_preferred(cluster, language=language))
                cluster = [article]
        kept.append(_preferred(cluster, language=language))

    return sorted(kept, key=lambda a: a.published_at, reverse=True)


def _preferred(bucket: list[Article], *, language: str) -> Article:
    if language == "ko":
        korean = [a for a in bucket if _HANGUL.search(a.title)]
        if korean:
            return korean[0]
    else:
        latin = [a for a in bucket if not _HANGUL.search(a.title)]
        if latin:
            return latin[0]
    return bucket[0]


def _richness(article: Article) -> int:
    return sum((
        bool(article.summary) * 4,
        article.score is not None and 2,
        bool(article.subtitle),
        bool(article.author),
    ))

"""Data models for configuration and for the pipeline's own records.

Two groups live here. The config models mirror the three files under `config/`
and exist so a typo in YAML fails at startup rather than three stages later.
The pipeline models are what fetchers produce and what the store writes.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

FetcherKind = Literal["feed", "embedded", "sitemap", "html_list"]


# ── Site registry ──────────────────────────────────────────────


class Site(BaseModel):
    """One row of `config/sites.csv`. Everything here is written by a person.

    `name` is the identity: it keys the state index and appears in the email,
    so renaming a site resets its recorded history.

    `enabled` answers "do I want this blog", which is not the same question as
    "is this blog still going". The second one is the pipeline's to answer and
    lives in `state.py`, so that a daily run never has to rewrite this file.
    """

    name: str
    url: str
    fetcher: FetcherKind
    source_url: str
    tz: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    note: str = ""


# ── Config ─────────────────────────────────────────────────────


class Interest(BaseModel):
    name: str
    keywords: list[str]
    special_instructions: str | None = None


class InterestsConfig(BaseModel):
    interests: list[Interest]
    special_instructions: str | None = None


class CollectionConfig(BaseModel):
    timezone: str = "Asia/Seoul"
    window_end_hour: int = Field(default=12, ge=0, le=23)
    window_hours: int = Field(default=24, ge=1)
    request_timeout: int = 30
    delay_between_requests: float = 0.3
    max_list_pages: int = 20
    inactive_after_days: int = 180
    recheck_inactive_every_days: int = 7


class RankingConfig(BaseModel):
    top_k: int = Field(default=5, ge=1)
    score_threshold: int = Field(default=5, ge=0, le=10)
    batch_size: int = Field(default=25, ge=1)


class LLMConfig(BaseModel):
    ranking_model: str = "gemini-2.5-flash-lite"
    summary_model: str = "gemini-2.5-flash"
    batch_delay: float = 5.0


class EmailConfig(BaseModel):
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    sender_address: str = ""
    sender_name: str = "IT Digest"
    recipients: list[str] = Field(default_factory=list)
    subject_format: str = "[IT Digest] {date} · {count}건 · {top_title} 외"
    show_failures: bool = True


class OutputConfig(BaseModel):
    language: str = "ko"
    data_dir: str = "data"


class Settings(BaseModel):
    collection: CollectionConfig = CollectionConfig()
    ranking: RankingConfig = RankingConfig()
    llm: LLMConfig = LLMConfig()
    email: EmailConfig = EmailConfig()
    output: OutputConfig = OutputConfig()


class AppConfig(BaseModel):
    """The three config files, resolved and validated together."""

    settings: Settings
    interests: InterestsConfig
    sites: list[Site]

    def enabled_sites(self) -> list[Site]:
        """Sites the reader wants. Whether each is *currently* worth collecting
        is a separate question, answered by `state.SiteStates.is_active`."""
        return [s for s in self.sites if s.enabled]


# ── Pipeline records ───────────────────────────────────────────


class Article(BaseModel):
    """One collected article.

    `url` is the deduplication key. `published_at` is always timezone-aware:
    fetchers resolve naive site dates with the site's `tz` before constructing
    this, so a comparison against the collection window means the same thing on
    a KST laptop and a UTC runner.

    The ranking fields stay unset until `rank.py` fills them, and `summary`
    until `summarize.py` does. They are serialized only when present, which is
    what keeps the stored artifact small.
    """

    site: str
    title: str
    url: str
    published_at: datetime
    author: str | None = None
    subtitle: str | None = None

    score: int | None = None
    interests: list[str] = Field(default_factory=list)
    reason: str | None = None
    summary: list[str] = Field(default_factory=list)

    # The article's text, when the source handed it over for free. Most feeds
    # carry the whole post in `content:encoded`, which makes summarizing it
    # cost nothing extra and works even where the site blocks page requests:
    # Medium answers a feed but returns 403 for the article page. Never
    # serialized, because storing bodies is what would make the daily
    # artifacts heavy.
    body: str = ""

    def to_record(self) -> dict[str, Any]:
        """Compact dict for JSONL. Empty and unset fields are dropped."""
        record: dict[str, Any] = {
            "published_at": self.published_at.isoformat(),
            "site": self.site,
            "title": self.title,
            "url": self.url,
        }
        for key in ("author", "subtitle", "reason"):
            value = getattr(self, key)
            if value:
                record[key] = value
        if self.score is not None:
            record["score"] = self.score
        for key in ("interests", "summary"):
            value = getattr(self, key)
            if value:
                record[key] = value
        return record


class FetchOutcome(BaseModel):
    """What a fetcher found: the articles in the window, and the site's pulse.

    `newest_seen` is the most recent publication date the fetcher observed
    anywhere in the source, not just inside the window. That distinction is
    what activity is judged on. A daily window returning nothing is the normal
    state of a blog that posts weekly, and reading it as evidence of a dead
    site would switch off most of the registry after one quiet day.
    """

    articles: list[Article] = Field(default_factory=list)
    newest_seen: datetime | None = None


class SiteResult(BaseModel):
    """What one site's collection attempt produced.

    A failure is recorded rather than raised so one dead blog cannot end the
    run, and so the email footer can report which sites went quiet.
    """

    site: str
    articles: list[Article] = Field(default_factory=list)
    newest_seen: datetime | None = None
    error: str | None = None
    examined: int = 0

    @property
    def ok(self) -> bool:
        return self.error is None

"""Render the digest as HTML and as plain text.

The plain-text part is not a formality. Many clients block remote content or
show the text alternative in previews, so it carries the same information in
the same order, and the bracketed tags survive into it unchanged.

Ranking scores stay out of both. They are recorded in the stored artifact,
where they can be checked against the ordering, but showing a number next to
every headline every morning adds noise without changing any decision.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from jinja2 import Environment, PackageLoader, select_autoescape

from it_newsletter.models import Article, EmailConfig, InterestsConfig, SiteResult
from it_newsletter.rank import order, select_top

WEEKDAYS = ("월", "화", "수", "목", "금", "토", "일")


def _environment(tz: ZoneInfo) -> Environment:
    env = Environment(
        loader=PackageLoader("it_newsletter", "templates"),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["kst"] = lambda dt: dt.astimezone(tz).strftime("%m-%d %H:%M")
    env.filters["kst_short"] = lambda dt: dt.astimezone(tz).strftime("%m-%d")
    return env


def date_label(moment: datetime, tz: ZoneInfo) -> str:
    local = moment.astimezone(tz)
    return f"{local:%Y-%m-%d} ({WEEKDAYS[local.weekday()]})"


def build(
    *,
    articles: list[Article],
    results: list[SiteResult],
    interests: InterestsConfig,
    email: EmailConfig,
    window_end: datetime,
    window_label: str,
    timezone: str,
    top_k: int,
    score_threshold: int,
    site_count: int,
    inactive_count: int,
) -> tuple[str, str, str]:
    """Return (subject, html, text).

    `articles` arrives ranked. The split into summarized cards and one-line
    links follows the same rule `summarize.py` used to choose what to summarize,
    so an article can never appear as a card without its summary.
    """
    tz = ZoneInfo(timezone)
    ranked = order(articles)

    top = select_top(articles, top_k=top_k, score_threshold=score_threshold)
    top_urls = {a.url for a in top}
    rest = [a for a in ranked if a.url not in top_urls]

    context = {
        "date_label": date_label(window_end, tz),
        "window_label": window_label,
        "total": len(articles),
        "site_count": site_count,
        "inactive_count": inactive_count,
        "top": top,
        "rest": rest,
        "interests": interests.interests,
        "failures": [r for r in results if not r.ok] if email.show_failures else [],
    }

    html = _environment(tz).get_template("newsletter.html").render(**context)
    text = _plain_text(context, tz)
    subject = email.subject_format.format(
        date=f"{window_end.astimezone(tz):%m-%d}",
        count=len(articles),
        top_title=top[0].title if top else "새 글 없음",
    )
    return subject, html, text


def _plain_text(context: dict, tz: ZoneInfo) -> str:
    lines = [
        f"{context['date_label']}  ·  {context['total']}건 / {context['site_count']}개 블로그",
        "=" * 60,
        "",
    ]

    if not context["top"] and not context["rest"]:
        lines.append("이 시간대에 올라온 글이 없습니다.")

    for index, article in enumerate(context["top"], start=1):
        tags = "".join(f"[{tag}]" for tag in article.interests)
        lines.append(f"{index}. {article.title} {tags}".rstrip())
        if article.summary:
            lines.append(f"   {' '.join(article.summary)}")
        elif article.subtitle:
            lines.append(f"   {article.subtitle[:140]}")
        byline = article.site + (f" · {article.author}" if article.author else "")
        lines.append(f"   {byline} · {article.published_at.astimezone(tz):%m-%d %H:%M}")
        lines.append(f"   {article.url}")
        lines.append("")

    if context["rest"]:
        lines.append(f"-- 나머지 {len(context['rest'])}건 " + "-" * 34)
        for article in context["rest"]:
            tags = "".join(f"[{tag}]" for tag in article.interests)
            lines.append(
                f"· {article.title} "
                f"({article.site} · {article.published_at.astimezone(tz):%m-%d}) {tags}".rstrip()
            )
            lines.append(f"  {article.url}")
        lines.append("")

    lines.append("-" * 60)
    lines.append(
        f"관심사 {len(context['interests'])}개 · "
        f"비활성 {context['inactive_count']}개 제외 · {context['window_label']}"
    )
    if context["failures"]:
        detail = ", ".join(f"{f.site} ({f.reason})" for f in context["failures"])
        lines.append(f"수집 실패 {len(context['failures'])}건: {detail}")
    return "\n".join(lines)

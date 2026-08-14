"""Summarize the top-ranked articles, and only those.

This is the one stage that costs a request to someone else's server per
article, so it runs over the top K rather than the whole window. An article
whose page cannot be read still keeps its rank and its link; it just arrives in
the email without a summary.

The output is two short Korean lines, matching the email design: dense enough
to decide whether to open the article, short enough that five of them fit on a
phone screen.
"""

from __future__ import annotations

import logging

from google import genai
from pydantic import BaseModel, Field

from it_newsletter.fetchers._common import Http, html_soup, main_text
from it_newsletter.llm import DailyQuotaExhausted, generate_json
from it_newsletter.models import Article, LLMConfig

logger = logging.getLogger(__name__)

# Enough of the body for the model to see what the article actually did,
# without paying for a 20,000-word migration retrospective in full.
BODY_LIMIT = 6000

SYSTEM_PROMPT = """당신은 개발자 한 명에게 매일 가는 기술 블로그 다이제스트의 요약자입니다.

주어진 글을 한국어 두 줄로 요약하세요. 독자는 이 두 줄만 보고 원문을 열지 말지 정합니다.

작성 규칙:
- `summary`는 1~2개 항목. 각 항목은 한 문장, 60자 내외.
- 글이 실제로 한 일을 쓰세요. 무엇을 겪었고, 무엇을 골랐고, 결과가 어땠는지.
- 제목을 바꿔 쓰지 마세요. 제목에 없는 정보를 담으세요.
- 홍보 문구, 채용 안내, 인사말은 버리세요.
- 본문에 없는 내용을 지어내지 마세요. 본문이 빈약하면 한 항목만 쓰세요.
- 번역체를 쓰지 마세요. 원문이 영어라도 자연스러운 한국어로 쓰세요."""


class Summary(BaseModel):
    summary: list[str] = Field(description="One or two short Korean sentences")


def summarize_top(
    client: genai.Client,
    articles: list[Article],
    llm: LLMConfig,
    *,
    top_k: int,
    score_threshold: int,
    http: Http,
) -> list[Article]:
    """Fill in `summary` on the best articles, in place. Returns the selection.

    Selection is by rank, then by threshold: an article nobody would want to
    read does not become worth reading by being the fifth-best of a quiet day.
    """
    chosen = [
        a for a in articles
        if (a.score or 0) >= score_threshold
    ][:top_k]

    if not chosen:
        logger.info("no article scored at least %d; nothing to summarize", score_threshold)
        return []

    for article in chosen:
        # The feed usually handed the body over already. Re-fetching it would
        # be a wasted request, and for Medium-hosted blogs it fails outright:
        # they serve the feed but answer the article page with a 403.
        body = article.body or _body(http, article.url)
        if not body:
            logger.warning("%s: could not read the body, leaving unsummarized", article.url)
            continue
        try:
            summary = _summarize_one(client, article, body, llm)
        except DailyQuotaExhausted as e:
            logger.error("%s; the rest arrive without a summary", e)
            break
        if summary:
            article.summary = summary

    summarized = [a for a in chosen if a.summary]
    logger.info("summarized %d of %d selected", len(summarized), len(chosen))
    return chosen


def _body(http: Http, url: str) -> str:
    try:
        return main_text(html_soup(http.get_text(url)), limit=BODY_LIMIT)
    except Exception as e:  # noqa: BLE001 - one unreadable article is not a failed run
        logger.warning("%s: %s: %s", url, type(e).__name__, e)
        return ""


def _summarize_one(
    client: genai.Client, article: Article, body: str, llm: LLMConfig
) -> list[str]:
    prompt = "\n".join([
        f"제목: {article.title}",
        f"출처: {article.site}",
        *( [f"작성자: {article.author}"] if article.author else [] ),
        *( [f"부제: {article.subtitle}"] if article.subtitle else [] ),
        "",
        "본문:",
        body,
    ])
    parsed = generate_json(
        client, model=llm.summary_model, system_prompt=SYSTEM_PROMPT,
        prompt=prompt, schema=Summary, temperature=0.3,
    )
    return [line.strip() for line in parsed.summary if line.strip()] if parsed else []

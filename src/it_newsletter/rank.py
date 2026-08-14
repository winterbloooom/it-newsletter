"""Score collected articles against the reader's interests, using Gemini.

Every article is scored. There is no keyword gate in front of the model, and
there was one: it skipped articles whose title contained no configured keyword,
which meant a post on grounding LLM answers in verified sources scored zero
because the reader had written "hallucination" and the author had written
"factuality". A day's window is a couple of dozen articles, one or two requests
to score in full, so the saving was never worth a rule that decides relevance
by string match before the model has read anything.

What the model sees is metadata: title, site, subtitle, and whatever summary
the feed carried. Article bodies are never fetched here. Only the top K, chosen
from these scores, are worth a second model call, and that happens in
`summarize.py`.

Keywords in `interests.yaml` are still used, as context in the prompt. They
describe an interest to the model rather than gating what reaches it, which is
why they can be written in one language while the articles arrive in another.
"""

from __future__ import annotations

import logging
import time

from google import genai
from pydantic import BaseModel, Field

from it_newsletter.llm import DailyQuotaExhausted, generate_json
from it_newsletter.models import Article, InterestsConfig, LLMConfig, RankingConfig

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You rank engineering-blog articles for one reader, against that reader's stated interests.

Score each article from 0 to 10 for how much this specific reader would want to read it:
- 8-10: squarely inside a stated interest, and reports something concrete (a system in production, numbers, an incident, a migration, a reversed decision).
- 5-7: inside a stated interest, but introductory, promotional, or thin on specifics.
- 2-4: adjacent to an interest without really being about it.
- 0-1: unrelated, a recruiting or event post, or a release note.

Rules:
- Judge from the title, subtitle and summary given. Do not invent content you were not shown.
- `interests` must contain only names copied exactly from the interest list, and only those the article genuinely matches. An article may match none; then use an empty list and a low score.
- `reason` is one short clause in Korean explaining the score. No full sentences, no restating the title.
- Score every article you are given, and return exactly one entry per index."""


class ArticleScore(BaseModel):
    index: int = Field(description="The article's index, exactly as given")
    score: int = Field(ge=0, le=10)
    interests: list[str] = Field(default_factory=list)
    reason: str = ""


class RankingResponse(BaseModel):
    scores: list[ArticleScore]


def _format_interests(interests: InterestsConfig) -> str:
    lines = []
    for interest in interests.interests:
        lines.append(f"- {interest.name}: {', '.join(interest.keywords)}")
        if interest.special_instructions:
            lines.append(f"    note: {interest.special_instructions}")
    if interests.special_instructions:
        lines.append("")
        lines.append(f"Applies to all interests: {interests.special_instructions}")
    return "\n".join(lines)


def _format_article(index: int, article: Article) -> str:
    parts = [f"[{index}] {article.title}", f"    site: {article.site}"]
    if article.author:
        parts.append(f"    author: {article.author}")
    if article.subtitle:
        parts.append(f"    summary: {article.subtitle}")
    return "\n".join(parts)



def rank(
    client: genai.Client,
    articles: list[Article],
    interests: InterestsConfig,
    ranking: RankingConfig,
    llm: LLMConfig,
) -> list[Article]:
    """Score every article and return them all, ordered best first.

    Nothing is discarded and nothing is skipped. A bad score sinks an article
    to the bottom of the email's link list, where the reader can still see it,
    and the stored artifact keeps the complete record of the window either way.
    """
    if not articles:
        return []

    logger.info("scoring %d article(s)", len(articles))
    candidates = articles
    interests_text = _format_interests(interests)

    for start in range(0, len(candidates), ranking.batch_size):
        batch = candidates[start: start + ranking.batch_size]
        if start:
            time.sleep(llm.batch_delay)
        logger.info("ranking %d-%d of %d", start + 1, start + len(batch), len(candidates))
        try:
            _apply_scores(
                client, batch, interests_text, llm,
                valid_names={i.name for i in interests.interests},
            )
        except DailyQuotaExhausted as e:
            # Everything scored so far is kept, and the rest fall to zero. A
            # partial ranking still delivers a useful digest.
            logger.error("%s; the remaining %d article(s) go unranked",
                         e, len(candidates) - start)
            break

    return sorted(
        articles,
        key=lambda a: (a.score or 0, a.published_at),
        reverse=True,
    )


def _apply_scores(
    client: genai.Client,
    batch: list[Article],
    interests_text: str,
    llm: LLMConfig,
    *,
    valid_names: set[str],
) -> None:
    """Score one batch in place. A failed batch leaves its articles at zero."""
    listing = "\n".join(_format_article(i, a) for i, a in enumerate(batch))
    prompt = (
        f"# The reader's interests\n\n{interests_text}\n\n"
        f"# Articles to score ({len(batch)})\n\n{listing}"
    )

    response = generate_json(
        client, model=llm.ranking_model, system_prompt=SYSTEM_PROMPT,
        prompt=prompt, schema=RankingResponse, temperature=0.2,
    )
    if response is None:
        for article in batch:
            article.score = article.score or 0
        return

    for entry in response.scores:
        if not 0 <= entry.index < len(batch):
            continue
        article = batch[entry.index]
        article.score = entry.score
        # Interest names become tag badges in the email, so a name the model
        # invented would be displayed as though it were configured. Keep only
        # the ones that exist.
        article.interests = [name for name in entry.interests if name in valid_names]
        article.reason = entry.reason or None


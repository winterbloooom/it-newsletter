"""The daily pipeline. This is what CI runs.

Order matters in two places. SMTP is authenticated before any model call, so a
bad password costs nothing on a free tier where a wasted run is a wasted day.
And the artifact is written before the email is sent, so a delivery failure
never loses a day's collection.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from google import genai

from it_newsletter import email_builder, sender, state, store
from it_newsletter.collect import collect
from it_newsletter.store import collapse_translations
from it_newsletter.config import get_gemini_api_key, get_smtp_password, load_config
from it_newsletter.fetchers._common import Http
from it_newsletter.rank import rank
from it_newsletter.sites_index import sites_to_collect
from it_newsletter.summarize import summarize_top
from it_newsletter.window import daily_window

logger = logging.getLogger(__name__)


def run(
    *,
    dry_run: bool,
    only: list[str] | None,
    preview: Path | None,
    from_store: date | None,
) -> int:
    config = load_config()
    settings = config.settings
    tz = ZoneInfo(settings.collection.timezone)
    now = datetime.now(tz)
    window = daily_window(settings.collection, now=now)
    logger.info("window: %s", window.label(tz))

    smtp_password = ""
    if not dry_run:
        smtp_password = get_smtp_password()
        sender.verify_auth(settings.email, smtp_password)

    if from_store is not None:
        ranked, results, targets = _replay(config, from_store)
    else:
        ranked, results, targets = _collect_and_rank(config, window, tz, now, only)

    subject, html, text = email_builder.build(
        articles=ranked,
        results=results,
        interests=config.interests,
        email=settings.email,
        window_end=window.end,
        window_label=window.label(tz),
        timezone=settings.collection.timezone,
        top_k=settings.ranking.top_k,
        score_threshold=settings.ranking.score_threshold,
        site_count=len(targets),
        inactive_count=len(config.sites) - len(targets),
    )

    if preview:
        preview.parent.mkdir(parents=True, exist_ok=True)
        preview.write_text(html, encoding="utf-8")
        preview.with_suffix(".txt").write_text(text, encoding="utf-8")
        logger.info("preview written to %s", preview)

    if dry_run:
        logger.info("dry run: not sending. Subject would be: %s", subject)
        return 0

    sender.send(
        settings.email, smtp_password,
        subject=subject, html_body=html, text_body=text,
    )
    return 0


def _collect_and_rank(config, window, tz, now, only):  # noqa: PLR0913
    """The normal path: read the sites, score, summarize, and store."""
    settings = config.settings

    # Inactive sites are still visited on a slower cadence, so a blog that
    # comes back to life is noticed rather than left switched off forever.
    states = state.load(settings.output.data_dir)
    targets = sites_to_collect(config.sites, states, settings.collection, today=now.date())
    if only:
        targets = [s for s in targets if s.name in only]
    logger.info("collecting %d of %d registered sites", len(targets), len(config.sites))

    known = store.known_urls(settings.output.data_dir, today=now.date())
    articles, results = collect(config, window, sites=targets, known_urls=known)

    client = genai.Client(api_key=get_gemini_api_key())
    ranked = rank(client, articles, config.interests, settings.ranking, settings.llm)
    summarize_top(
        client, ranked, settings.llm,
        top_k=settings.ranking.top_k,
        score_threshold=settings.ranking.score_threshold,
        http=Http(
            timeout=settings.collection.request_timeout,
            delay=settings.collection.delay_between_requests,
        ),
    )

    path = store.merge_write(
        store.daily_path(settings.output.data_dir, window.end.astimezone(tz).date()),
        ranked,
    )
    logger.info("stored %d article(s) in %s", len(ranked), path)

    # What the run learned goes to the state cache, never to config/sites.csv.
    # The registry is hand-edited, and a job that rewrites it every morning
    # cannot coexist with someone adding a site.
    state.update(states, results)
    state.save(states, settings.output.data_dir)
    return ranked, results, targets


def _replay(config, day: date):
    """Rebuild the email from a stored day, touching neither sites nor Gemini.

    Delivery is the one stage that can fail after all the expensive work is
    done. Replaying makes a retry cost nothing, which matters on a free tier
    where re-running the pipeline would spend a second day's quota to produce
    the identical digest.

    Collection failures are not recovered, because they are a property of the
    run rather than of an article and so were never stored. A replayed email
    therefore has no failure line in its footer.
    """
    path = store.daily_path(config.settings.output.data_dir, day)
    articles = store.read(path)
    if not articles:
        raise FileNotFoundError(f"nothing stored for {day} at {path}")

    articles = collapse_translations(
        store.dedupe(articles), language=config.settings.output.language
    )
    logger.info("replaying %d stored article(s) from %s", len(articles), path.name)
    return articles, [], config.enabled_sites()


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect, rank, summarize and email the daily digest")
    parser.add_argument("--dry-run", action="store_true",
                        help="run everything except sending")
    parser.add_argument("--only", metavar="SITE", action="append",
                        help="restrict to one site by name; repeatable, for testing")
    parser.add_argument("--preview", type=Path, metavar="PATH",
                        help="also write the rendered email to PATH (and PATH.txt)")
    parser.add_argument("--from-store", metavar="YYYY-MM-DD", nargs="?", const="today",
                        help="rebuild and send from a stored day instead of collecting; "
                             "no site requests and no model calls")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("google_genai").setLevel(logging.WARNING)

    try:
        from_store = None
        if args.from_store:
            from_store = (
                _today_window_date() if args.from_store == "today"
                else date.fromisoformat(args.from_store)
            )
        sys.exit(run(dry_run=args.dry_run, only=args.only,
                     preview=args.preview, from_store=from_store))
    except Exception:
        logger.exception("pipeline failed")
        sys.exit(1)


def _today_window_date() -> date:
    """The date the current window's stored file is named after."""
    config = load_config()
    tz = ZoneInfo(config.settings.collection.timezone)
    return daily_window(config.settings.collection).end.astimezone(tz).date()


if __name__ == "__main__":
    main()

# IT Newsletter

## Project Purpose

Collect every article published by a tracked list of tech blogs in a fixed daily
window, rank them against the reader's registered interests, summarize the top K,
and deliver one email. Everything collected is written to a lightweight artifact
so past days can be queried later without re-crawling.

## Architecture

Five stages, run daily by GitHub Actions: **collect → rank → summarize → send → store**.

| Module / dir | Purpose |
|--------------|---------|
| `config/interests.yaml` | The reader's interests. The only input that decides ranking. |
| `config/settings.yaml` | Operating parameters: collection window, `top_k`, Gemini models, SMTP host. Tracked, and holds no personal data. |
| `config/sites.csv` | Site registry, human-owned: name, URL, how to collect it, `enabled`. Committed. |
| `.env` | Everything private: `GEMINI_API_KEY`, `SMTP_PASSWORD`, `MAIL_SENDER`, `MAIL_RECIPIENTS`. Gitignored; repository secrets of the same names in CI. |
| `src/it_newsletter/config.py` | Loads the three config files plus `.env`. Every tunable value enters here. |
| `src/it_newsletter/models.py` | `Article`, `Site`, and the config models. |
| `src/it_newsletter/window.py` | Resolves a collection window to an absolute UTC interval. |
| `src/it_newsletter/collect.py` | Walks the site registry, dispatches to a fetcher, isolates per-site failure. |
| `src/it_newsletter/fetchers/_common.py` | HTTP session, date parsing, HTML and meta extraction. Shared by every fetcher. |
| `src/it_newsletter/fetchers/feed.py` | RSS / Atom / JSON Feed. Covers most of the registry. |
| `src/it_newsletter/fetchers/embedded.py` | JSON embedded in the page: JSON-LD, `__NEXT_DATA__`, RSC payloads. |
| `src/it_newsletter/fetchers/html_list.py` | Config-driven fallback: list-page anchors plus detail-page metadata. |
| `src/it_newsletter/fetchers/discover.py` | Probes a new site and reports which fetcher and parameters it needs. |
| `src/it_newsletter/llm.py` | The single Gemini call path: structured output, and the free tier's two rate limits. |
| `src/it_newsletter/rank.py` | Gemini batch relevance scoring over collected metadata. |
| `src/it_newsletter/summarize.py` | Summarizes the top K, from the feed's body where there is one. |
| `src/it_newsletter/email_builder.py` | Renders the HTML and plain-text email. |
| `src/it_newsletter/sender.py` | SMTP delivery. |
| `src/it_newsletter/store.py` | Reads and writes the JSONL artifacts under `data/`. |
| `src/it_newsletter/state.py` | What the run learned per site. Cached in `data/`, never committed. |
| `src/it_newsletter/sites_index.py` | Derives which sites are worth collecting today. |
| `src/it_newsletter/cli/daily.py` | The daily pipeline. What CI runs. |
| `src/it_newsletter/cli/scan.py` | Ad-hoc range queries ("last N", "last year"). Collect and store only, no email. |
| `scripts/scan.sh` | Shell wrapper over `cli/scan.py`. |
| `.github/workflows/daily.yml` | Scheduled run at KST 12:00. |

**Key design decisions.** Non-obvious choices and their reasoning, especially ones that would look wrong or arbitrary without context:

- **No per-site parsers.** A probe of the 33 sample sites found 27 served a usable
  standard feed, 3 embedded their post list as JSON in the page, and 2 needed
  list-plus-detail HTML scraping. Only one (`krafton-ai.github.io`, which hardcodes
  its posts in an inline `const POSTS = [...]`) resists all three. So the registry
  names a fetcher and its parameters; writing code is the last resort, not the first.
- **A feed counts only if it yields entries.** `tech.kakaopay.com/rss.xml` is
  well-formed RSS with zero items. Treating "parses as XML" as success makes that
  site silently report nothing every day, forever. Fetchers assert entry count.
- **`data/` is a cache, not the source of truth.** Range queries read what is
  already stored and re-crawl only the gaps, so an expired or absent artifact costs
  time, never correctness. This is why CI can upload to Actions artifacts (which
  expire) without endangering the "last year" query.
- **Rank on metadata, fetch bodies only for the top K.** Ranking reads title,
  subtitle, tags and whatever summary the feed already carries. Only the K articles
  that will actually appear in the email cost an extra request and a second
  Gemini call. This keeps the daily run inside the free tier's rate limits.
- **The collection window is absolute, and articles are deduplicated by URL.**
  Sites report dates at varying precision (some only to the day), so a fixed
  KST 12:00 boundary would otherwise double-count or drop articles near the edge.
- **A blog is one registry row even when it spans several pages.** `is_active`
  and the site's name describe the blog, not a section of it, so pagination
  (`page_url`) and additional sections (`extra_sources`) are parameters rather
  than extra rows. Paging stops when a list page reaches back past the window,
  not at a page count, which keeps a daily run at one page per site while
  letting a scan reach years back.
- **Personal data lives only in `.env`.** The sender and recipient addresses are
  read from the environment rather than from `settings.yaml`, so the tracked
  configuration carries operating parameters and nothing else. That keeps the
  parameters reviewable in git history while leaving no address in a committed
  file. A value in the YAML still wins when the environment does not set one.
- **A site's activity is derived, not stored in the registry.** `config/sites.csv`
  is hand-edited, so the daily run must never write to it: with `last_post_date`
  in the file, a busy day changed 56 of 135 rows. What the run learns goes to
  `data/sites-state.json`, which is cached rather than committed because losing
  it costs exactly one run to rebuild.
- **Activity is judged on the newest post a fetcher saw, not on the window.**
  A blog that posts weekly returns nothing on most days, and reading that as
  death switches off the registry. Fetchers report `newest_seen` separately, and
  an unknown date is never evidence of a dead site.

## Environment

- **Python 3.11 or newer**, in a virtual environment. `zoneinfo` and `tomllib` are
  used from the standard library, and 3.11's `datetime.fromisoformat` is what makes
  the shared date parser cover both ISO 8601 and the loose formats sites emit.
- **Never install into the system Python.** After activating the venv, confirm the
  interpreter before installing: `which python && which pip` must point inside
  `.venv/`. Prefer `python -m pip install -e .`.
- **Gemini runs on the free tier.** Rate limits are per-minute and per-day. When
  testing, set `top_k` to 1 and point `sites.csv` at a couple of sites; a full run
  over the whole registry is not a test, it is a day's quota.

## Principles

- **No duplicated logic (the rule broken most often).** Split the code into modules along clear lines of responsibility, and declare each function once in the module that owns its concern; elsewhere, call it with different arguments. Before writing new code, check whether it already exists to call; don't ship a copy or re-implementation, propose the shared function instead. When you spot duplication already in the code, flag it and consolidate it.
- **Ground decisions in authoritative sources.** Base fixes and directions on primary references (official docs, reputable projects, logs/output), not guesses. Distrust prior analysis, even your own, until verified against one. Mark inference as inference; when evidence is thin, investigate, ask, or say "I don't know". The same deference applies to code: prefer the standard library, then the platform, then a dependency the project already has, over writing your own. A new dependency comes last, and only an established, well-maintained one.
- **Follow existing convention first.** For non-trivial changes, check prior art (this project's conventions and how established tools solve the same problem) and follow that. Surface what you found before implementing, and get a go-ahead first.
- **Think before coding.** State your assumptions; ask when uncertain. Surface multiple interpretations instead of silently picking one, and push back if a simpler approach exists.
- **Simplicity first.** Write the minimum code that solves the problem: no speculative features, no unrequested configurability, no error handling for impossible cases. Simplicity never comes out of input validation at a trust boundary, error handling that prevents data corruption, security, or accessibility.
- **Decide architecture for the long run.** Judge a structural choice by where it leads, not by what clears today's task. Don't accept a stopgap that only defers the problem and is meant to be swapped out later.

## Workflow

- **Commit per feature unit.** One commit = one intent, shipping with its tests. Commit only at real milestones, never without explicit confirmation. For message format, granularity, and branch naming, follow the `git-workflow` skill.
- **Keep docs in sync.** Surface when: (1) a change alters outward-facing behavior (CLI flags, setup, env vars, output format); (2) you notice a doc that contradicts the code.
- **Track large work in a plan file.** For multi-turn work (refactors/migrations, features needing design decisions), create `plans/<short-kebab-name>.md` and keep it current as you go; the `plan-file` skill covers what belongs in it. Skip it for single-file edits and one-shot fixes.

## Communication

- **Explain plainly. This is routinely skipped, so treat it as mandatory.** Open every technical explanation with a plain-language version a non-expert could follow, then go deep. Expand abbreviations on first use. An established term of art keeps the form the field uses. Explain it in the conversation's language rather than translating the term itself.
- **Explanation level.** The reader is a deep learning research engineer, expert in modeling but unfamiliar with everything outside it: distributed training, infrastructure, hardware, low-level optimization, software engineering practice. Keep the depth at expert level and lower only the vocabulary. Establish prerequisite ideas before the main body, and state the problem before the solution that addresses it.
- **Write code comments in English.** Docstrings and inline comments in source files are always English, regardless of the language used in conversation.
- **Write docs and comments as a present-tense snapshot, not a changelog.** State what's true now and revise in place: edit the sentence that's now wrong rather than appending "(updated…)" or "previously X, now Y". History belongs in git log, not the doc.
- **No em dashes in prose.** Use a comma, a colon, parentheses, or a separate sentence instead.

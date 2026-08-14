"""The single place this project calls Gemini.

Both ranking and summarization want the same thing: one structured JSON answer,
with the free tier's rate limits handled. Keeping that in one function means
the retry policy is defined once, and a change to it cannot apply to one stage
and not the other.

The free tier enforces two separate limits, and they need opposite responses:

  per minute   waiting is the correct answer, and the error carries the delay
               to wait for.
  per day      waiting is useless. `gemini-2.5-flash` allows 20 requests a day,
               so once that is gone the run should give up on the remaining
               calls and deliver what it already has, rather than sleeping
               through a CI timeout for nothing.
"""

from __future__ import annotations

import logging
import re
import time
from typing import TypeVar

from google import genai
from google.genai import types
from google.genai.errors import ClientError
from pydantic import BaseModel

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

MAX_ATTEMPTS = 3
DEFAULT_RETRY_DELAY = 30.0

_RETRY_DELAY = re.compile(r"retryDelay['\"]?\s*[:=]\s*['\"]?(\d+(?:\.\d+)?)")
_PER_DAY = re.compile(r"PerDay|per day|GenerateRequestsPerDay", re.I)


class DailyQuotaExhausted(RuntimeError):
    """The per-day free-tier allowance for this model is spent."""


def generate_json(
    client: genai.Client,
    *,
    model: str,
    system_prompt: str,
    prompt: str,
    schema: type[T],
    temperature: float = 0.2,
) -> T | None:
    """One structured call. Returns None if it failed for a recoverable reason.

    Raises `DailyQuotaExhausted` so the caller can stop making calls that are
    all going to fail. Every other failure returns None, because one bad batch
    or one unreadable article should not end the run.
    """
    for attempt in range(MAX_ATTEMPTS):
        try:
            response = client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    response_mime_type="application/json",
                    response_schema=schema,
                    temperature=temperature,
                ),
            )
            return response.parsed
        except ClientError as e:
            if e.code != 429:
                logger.exception("%s call failed", model)
                return None
            message = str(e)
            if _PER_DAY.search(message):
                raise DailyQuotaExhausted(
                    f"{model}: the free tier's daily request allowance is spent"
                ) from e
            if attempt == MAX_ATTEMPTS - 1:
                logger.warning("%s: still rate limited after %d attempts", model, MAX_ATTEMPTS)
                return None
            delay = _retry_delay(message)
            logger.warning("%s: rate limited, waiting %.0fs", model, delay)
            time.sleep(delay)
        except Exception:
            logger.exception("%s call failed", model)
            return None
    return None


def _retry_delay(message: str) -> float:
    """The delay the server asked for, plus a margin, or a sane default."""
    match = _RETRY_DELAY.search(message)
    return float(match.group(1)) + 2 if match else DEFAULT_RETRY_DELAY

"""Proactive token pacing for Groq's per-minute budget.

The investigation agent already retries after a 429 (BUILD_LOG 2026-09-02-08),
which is the right safety net but the wrong primary strategy for a 200-case
run: every 429 is a wasted round trip, and the backoff is reactive guesswork.

This paces requests *before* sending, against a rolling 60-second token
window, so a long evaluation run mostly never hits the limit at all. It is
deliberately conservative - it would rather idle a second too long than eat a
rate-limit error and the retry that follows.

Groq's free tier for openai/gpt-oss-120b was measured at 8,000 tokens/minute
(observed directly in a live 429 body during Phase 4, not guessed).
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Observed live from a Groq 429 response body during Phase 4.
DEFAULT_TOKENS_PER_MINUTE = 8_000

# Rough tokens-per-character for English prose + JSON. Deliberately pessimistic
# (real ratio is nearer 1/4); over-estimating costs a little idle time, while
# under-estimating costs a 429 and a retry.
_CHARS_PER_TOKEN = 3.6

# Headroom for the model's own output, which also counts against the budget
# and which we cannot measure before the call.
DEFAULT_OUTPUT_ALLOWANCE = 900


def estimate_tokens(text: str, output_allowance: int = DEFAULT_OUTPUT_ALLOWANCE) -> int:
    """Estimate the token cost of a request, input + expected output."""
    return int(len(text) / _CHARS_PER_TOKEN) + output_allowance


@dataclass
class PacerStats:
    requests: int = 0
    tokens_spent: int = 0
    seconds_waited: float = 0.0
    waits: int = 0


@dataclass
class TokenBudgetPacer:
    """Rolling-window limiter. Call `acquire(n)` before each request.

    Not a precise accounting of Groq's own counter - it can't be, since the
    real output size isn't known in advance. It's a governor: keep the send
    rate under the budget so the reactive backoff almost never fires.
    """

    tokens_per_minute: int = DEFAULT_TOKENS_PER_MINUTE
    # Only spend this fraction of the stated budget. The estimate is rough and
    # the window boundary is fuzzy, so leaving room is cheaper than a 429.
    safety_margin: float = 0.80
    window_seconds: float = 60.0
    stats: PacerStats = field(default_factory=PacerStats)
    _events: deque = field(default_factory=deque, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def budget(self) -> int:
        return int(self.tokens_per_minute * self.safety_margin)

    def _prune(self, now: float) -> None:
        while self._events and now - self._events[0][0] >= self.window_seconds:
            self._events.popleft()

    def _spent(self) -> int:
        return sum(tokens for _, tokens in self._events)

    def acquire(self, tokens: int) -> float:
        """Block until `tokens` fit inside the budget. Returns seconds waited.

        A single request larger than the whole budget is let through rather
        than deadlocking - it will hit the API's own limit and the agent's
        backoff will deal with it. Better a slow request than a hung run.
        """
        waited = 0.0
        with self._lock:
            if tokens >= self.budget:
                logger.warning(
                    "single request estimated at %d tokens exceeds the %d budget; "
                    "sending anyway and relying on backoff", tokens, self.budget,
                )
            else:
                while True:
                    now = time.monotonic()
                    self._prune(now)
                    if self._spent() + tokens <= self.budget:
                        break
                    # Sleep exactly until the oldest event leaves the window.
                    oldest_at = self._events[0][0]
                    sleep_for = max(0.05, self.window_seconds - (now - oldest_at) + 0.05)
                    logger.info(
                        "pacing: %d/%d tokens used in window, waiting %.1fs",
                        self._spent(), self.budget, sleep_for,
                    )
                    time.sleep(sleep_for)
                    waited += sleep_for
                    self.stats.waits += 1

            self._events.append((time.monotonic(), tokens))
            self.stats.requests += 1
            self.stats.tokens_spent += tokens
            self.stats.seconds_waited += waited
        return waited

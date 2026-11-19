"""Rate limits for break-glass, as plain arithmetic shared by every store."""
from __future__ import annotations

from typing import List, Tuple

DAY = 86400
# An idempotency record still "pending" after this long was abandoned by an
# invocation that died; the key may be used again.
IDEM_PENDING_SECONDS = 60
INTERVAL = "interval"
DAILY_CAP = "daily_cap"


def decide(
    free_at: int, sent: List[int], now: int, daily_cap: int
) -> Tuple[bool, int, str, List[int]]:
    """Can an alert go out now?

    free_at is when the minimum interval since the last alert ends; sent holds
    the times of earlier alerts. Returns (allowed, seconds to wait, reason,
    the alerts still inside the last 24 hours).
    """
    recent = sorted(t for t in sent if t > now - DAY)
    if now < free_at:
        return False, free_at - now, INTERVAL, recent
    if daily_cap and len(recent) >= daily_cap:
        return False, max(recent[0] + DAY - now, 1), DAILY_CAP, recent
    return True, 0, "", recent

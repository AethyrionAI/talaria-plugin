"""#363 outbox hygiene: artifact-kind rows are content-sized (#362's
mirror), and the outbox retains rows forever by design — so delivered
artifact rows SCRUB (text blanked, deactivated; row + meta + delivered_at
kept as the audit trail — the phone staged its own copy at ack) and
undelivered ones EXPIRE (deactivated, content intact) at seven days.
Deactivate/blank, never DELETE — the #144 shape. Message-kind rows are
deliberately untouched in v0.

Triggers: gateway startup (``register()``, wrapped), a throttled
opportunistic pass after a mirror append (``maybe_sweep`` — one monotonic
read on the hot path, and it never raises), and ``hermes talaria prune``
with ``--dry-run``.

Scrubbed is a derived state, not a column:
``kind='artifact' AND delivered_at IS NOT NULL AND active=0 AND text=''``.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

from .database import connect

logger = logging.getLogger("talaria")

RETENTION_DAYS = 7.0
_THROTTLE_SECONDS = 6 * 3600.0
_last_sweep_at: float | None = None


def _cutoff_iso(now: datetime | None = None) -> str:
    reference = now or datetime.now(timezone.utc)
    return (reference - timedelta(days=RETENTION_DAYS)).isoformat()


def sweep(*, dry_run: bool = False) -> dict:
    """One transaction, two arms, counts returned. The store's ISO-8601
    UTC timestamps compare lexicographically in chronological order, so
    the cutoffs are plain string comparisons."""
    cutoff = _cutoff_iso()
    scrub_where = (
        "kind = 'artifact' AND delivered_at IS NOT NULL "
        "AND delivered_at < ? AND (active = 1 OR text != '')"
    )
    expire_where = (
        "kind = 'artifact' AND delivered_at IS NULL "
        "AND created_at < ? AND active = 1"
    )
    connection = connect()
    try:
        if dry_run:
            scrubbed = connection.execute(
                f"SELECT COUNT(*) FROM outbox_items WHERE {scrub_where}", (cutoff,)
            ).fetchone()[0]
            expired = connection.execute(
                f"SELECT COUNT(*) FROM outbox_items WHERE {expire_where}", (cutoff,)
            ).fetchone()[0]
            return {"scrubbed": scrubbed, "expired": expired}
        connection.execute("BEGIN IMMEDIATE")
        scrubbed = connection.execute(
            f"UPDATE outbox_items SET text = '', active = 0 WHERE {scrub_where}",
            (cutoff,),
        ).rowcount
        expired = connection.execute(
            f"UPDATE outbox_items SET active = 0 WHERE {expire_where}",
            (cutoff,),
        ).rowcount
        connection.commit()
        if scrubbed or expired:
            logger.info(
                "talaria hygiene: scrubbed %d delivered / expired %d undelivered artifact rows",
                scrubbed, expired,
            )
        return {"scrubbed": scrubbed, "expired": expired}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def maybe_sweep(now=time.monotonic) -> None:
    """The opportunistic trigger — at most one sweep per 6 h, and it never
    raises (it rides the mirror hook's tool-dispatch path)."""
    global _last_sweep_at
    try:
        stamp = now()
        if _last_sweep_at is not None and stamp - _last_sweep_at < _THROTTLE_SECONDS:
            return
        _last_sweep_at = stamp
        sweep()
    except Exception:
        logger.debug("talaria hygiene sweep skipped", exc_info=True)

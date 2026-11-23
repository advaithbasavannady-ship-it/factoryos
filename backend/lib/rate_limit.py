"""Mongo-backed fixed-window throttling for authentication endpoints.

The Mongo document's built-in unique ``_id`` makes increments atomic across
application workers. An explicit TTL index is declared in ``lib.db`` for cleanup.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

from pymongo import ReturnDocument

async def consume_rate_limit(scope: str, subject: str, *, limit: int, window_seconds: int,
                             now: datetime | None = None, collection=None) -> tuple[bool, int]:
    if limit <= 0 or window_seconds <= 0:
        raise ValueError("rate limit and window must be positive")
    instant = now or datetime.now(timezone.utc)
    epoch = int(instant.timestamp())
    window = epoch // window_seconds
    subject_hash = hashlib.sha256(subject.encode("utf-8")).hexdigest()
    bucket_id = hashlib.sha256(f"{scope}:{subject_hash}:{window}".encode("ascii")).hexdigest()
    expires_at = datetime.fromtimestamp((window + 2) * window_seconds, timezone.utc)
    if collection is None:
        from lib.db import db
        collection = db.rate_limits
    row = await collection.find_one_and_update(
        {"_id": bucket_id},
        {"$inc": {"count": 1}, "$setOnInsert": {"scope": scope, "expires_at": expires_at}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    count = int(row["count"])
    retry_after = max(1, (window + 1) * window_seconds - epoch)
    return count <= limit, retry_after

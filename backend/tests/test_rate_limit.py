"""Pure tests for auth throttling policy; no database or network is used."""

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone

from lib.rate_limit import consume_rate_limit


class FakeRateLimitCollection:
    def __init__(self):
        self.rows = {}

    async def find_one_and_update(self, query, update, *, upsert, return_document):
        row = self.rows.setdefault(query["_id"], {"_id": query["_id"], **deepcopy(update["$setOnInsert"])})
        row["count"] = row.get("count", 0) + update["$inc"]["count"]
        return deepcopy(row)


def test_fixed_window_is_shared_per_subject_and_rolls_over():
    async def scenario():
        collection = FakeRateLimitCollection()
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        assert await consume_rate_limit("auth.login.ip", "192.0.2.2", limit=2,
                                        window_seconds=60, now=start, collection=collection) == (True, 60)
        assert await consume_rate_limit("auth.login.ip", "192.0.2.2", limit=2,
                                        window_seconds=60, now=start, collection=collection) == (True, 60)
        assert await consume_rate_limit("auth.login.ip", "192.0.2.2", limit=2,
                                        window_seconds=60, now=start, collection=collection) == (False, 60)
        assert await consume_rate_limit("auth.login.ip", "192.0.2.2", limit=2,
                                        window_seconds=60, now=start + timedelta(minutes=1),
                                        collection=collection) == (True, 60)

    asyncio.run(scenario())

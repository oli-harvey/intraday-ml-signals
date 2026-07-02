"""Async cold-path logger.

Appends raw ticks, predictions, and orders to DuckDB/SQLite for offline research and
replay. Fed by a non-blocking tap off the live queues so the decision loop never waits
on disk. Reads happen only offline. Phase 5 — see docs/PLAN.md.
"""

from __future__ import annotations

import asyncio


class ColdStore:
    def __init__(self, path: str = "data/ticks.duckdb") -> None:
        self.path = path

    async def run(self, queue: asyncio.Queue) -> None:
        """Drain the tap queue and batch-append to storage."""
        raise NotImplementedError("Phase 5")

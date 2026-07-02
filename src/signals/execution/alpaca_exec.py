"""Alpaca paper-trading executor.

Routes orders through the Alpaca PAPER endpoint only (no real capital). Tracks fills
and positions; supports a kill-switch that flattens everything on shutdown.
Phase 4 — see docs/PLAN.md.
"""

from __future__ import annotations

from ..config import AlpacaConfig
from ..signal.policy import Signal


class PaperExecutor:
    def __init__(self, config: AlpacaConfig) -> None:
        if not config.is_paper:
            raise RuntimeError("Refusing to run: config does not point at paper trading.")
        self.config = config

    async def submit(self, signal: Signal, qty: float) -> None:
        raise NotImplementedError("Phase 4")

    async def flatten_all(self) -> None:
        """Kill-switch: close every open position."""
        raise NotImplementedError("Phase 4")

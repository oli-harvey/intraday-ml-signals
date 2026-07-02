"""Runtime configuration loaded from environment / .env.

Keep this dependency-light; it is imported by the hot path.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True, slots=True)
class AlpacaConfig:
    api_key: str
    secret_key: str
    base_url: str = "https://paper-api.alpaca.markets"
    data_feed: str = "iex"

    @property
    def is_paper(self) -> bool:
        return "paper" in self.base_url


def load_alpaca_config() -> AlpacaConfig:
    """Read Alpaca paper credentials from the environment.

    Raises if keys are missing so we fail fast rather than mid-stream.
    """
    key = os.environ.get("ALPACA_API_KEY", "")
    secret = os.environ.get("ALPACA_SECRET_KEY", "")
    if not key or not secret:
        raise RuntimeError(
            "ALPACA_API_KEY / ALPACA_SECRET_KEY not set. Copy .env.example -> .env."
        )
    return AlpacaConfig(
        api_key=key,
        secret_key=secret,
        base_url=os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets"),
        data_feed=os.environ.get("ALPACA_DATA_FEED", "iex"),
    )

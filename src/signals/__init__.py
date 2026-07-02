"""intraday-ml-signals: online-learning intraday trade-signal engine.

Hot path (ingest -> features -> inference -> decision) must stay O(1) per tick and
free of pandas. See docs/ARCHITECTURE.md and docs/PLAN.md.
"""

__version__ = "0.0.1"

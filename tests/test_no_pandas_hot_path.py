"""Cross-cutting invariant: no pandas anywhere under src/signals/ (the hot path).

pandas arrives transitively via alpaca-py, so we can't ban it from the environment —
instead we ban importing it in our live-loop code. Offline analysis lives outside
src/signals/ (scripts/notebooks) and may use pandas freely.
"""

import re
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "signals"
IMPORT_PANDAS = re.compile(r"^\s*(import pandas\b|from pandas\b)", re.MULTILINE)


def test_no_pandas_import_in_hot_path() -> None:
    offenders = [
        str(p.relative_to(SRC))
        for p in SRC.rglob("*.py")
        if IMPORT_PANDAS.search(p.read_text(encoding="utf-8"))
    ]
    assert not offenders, f"pandas imported in hot-path modules: {offenders}"

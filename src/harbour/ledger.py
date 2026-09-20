"""The gateway ledger: one row per model call, with tokens and an
estimated dollar cost, keyed by case_id and a call_id that matches a span
in the trace export. This is what turns "what does resolving this case
cost" from a day of grepping logs into `ledger_summary_for_case(...)`.

Rates are a simple, documented, swappable table -- not a claim about any
specific provider's real pricing, since this build has no live gateway to
get real rates from. See llm_client.py for where these numbers actually
originate (synthetic, token-count-based, clearly labelled as such).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

# USD per 1,000 tokens. A stand-in rate table, not a real provider's
# pricing -- documented here once so nothing downstream has to guess
# where a dollar figure came from.
RATE_PER_1K_INPUT_TOKENS_USD = 0.003
RATE_PER_1K_OUTPUT_TOKENS_USD = 0.015


@dataclass(frozen=True)
class LedgerEntry:
    case_id: str
    call_id: str
    model_id: str
    tokens_in: int
    tokens_out: int
    cost_usd: float
    created_at: float


def estimate_cost_usd(tokens_in: int, tokens_out: int) -> float:
    cost = (tokens_in / 1000) * RATE_PER_1K_INPUT_TOKENS_USD
    cost += (tokens_out / 1000) * RATE_PER_1K_OUTPUT_TOKENS_USD
    return round(cost, 8)


class LedgerWriter:
    def __init__(self, export_path: str | Path):
        self.export_path = Path(export_path)
        self.export_path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, entry: LedgerEntry) -> None:
        with self.export_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(entry)) + "\n")


def read_ledger(export_path: str | Path) -> list[dict[str, Any]]:
    path = Path(export_path)
    if not path.exists():
        return []
    entries = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return entries


def cost_summary_by_case(export_path: str | Path) -> dict[str, dict[str, float]]:
    """Return {case_id: {"tokens_in": n, "tokens_out": n, "cost_usd": n, "calls": n}}.
    This is the function that answers "what did resolving this case
    cost" and "what's the median/worst case cost across a run" -- the two
    questions the brief says the team spent a day guessing at.
    """
    summary: dict[str, dict[str, float]] = {}
    for entry in read_ledger(export_path):
        bucket = summary.setdefault(
            entry["case_id"], {"tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0, "calls": 0}
        )
        bucket["tokens_in"] += entry["tokens_in"]
        bucket["tokens_out"] += entry["tokens_out"]
        bucket["cost_usd"] += entry["cost_usd"]
        bucket["calls"] += 1
    for bucket in summary.values():
        bucket["cost_usd"] = round(bucket["cost_usd"], 8)
    return summary

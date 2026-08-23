"""Скоринг можливостей.

Свідомо НЕ одна цифра «прибутковість»: бот має віддавати перевагу угодам,
які (а) мають едж, (б) переживають найгірший сценарій, (в) реально
виконуються за розміром, (г) не мають структурних розривів (експірація,
джерело резолву).
"""
from __future__ import annotations

import math

from ..models import Opportunity


def score(op: Opportunity, min_liquidity_usd: float = 1_000.0) -> float:
    m = op.metrics
    if m.capital <= 0:
        return 0.0

    edge = max(m.edge_pp, 0.0) / 10.0                     # 10 в.п. едж -> 1.0
    ret = max(m.ev_apr, 0.0)                              # очікувана дохідність, річних
    safety = 1.0 / (1.0 + max(-m.worst_return, 0.0) * 10) # -10% найгірший -> 0.5
    liq = min(m.liquidity_usd / min_liquidity_usd, 3.0) / 3.0 if m.liquidity_usd else 0.2
    penalty = 1.0
    for flag in m.flags:
        if flag.startswith("expiry_gap"):
            penalty *= 0.6
        elif flag.startswith("extrapolated_iv"):
            penalty *= 0.7
        elif flag.startswith("unknown_resolution"):
            penalty *= 0.4
        elif flag.startswith("thin"):
            penalty *= 0.7
        elif flag.startswith("verify_race_direction"):
            # напрямок YES/NO для "яка ціна раніше" — регекс, вища ставка
            # переплутати, ніж для звичайного touch; людина мусить звірити
            penalty *= 0.5
    return (0.4 * edge + 0.4 * math.tanh(ret) + 0.2 * liq) * safety * penalty


def rank(ops: list[Opportunity], min_liquidity_usd: float = 1_000.0) -> list[Opportunity]:
    for op in ops:
        op.score = score(op, min_liquidity_usd)
    return sorted(ops, key=lambda o: o.score, reverse=True)

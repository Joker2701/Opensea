"""Генератор кандидатів-конструкцій.

Одна пара «ринок предикшену × опціонний ланцюг» породжує десятки
варіантів (страйк, тип хеджа, експірація). Тут ми їх ПОРОДЖУЄМО,
а sizing.py вирішує, чи існує розмір, за якого вилка справді є.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Iterable, Optional

from ..models import (
    ClaimKind,
    OptionChain,
    OptionQuote,
    OptionType,
    Outcome,
    PredictionMarket,
    Side,
)


@dataclass
class OptionSpec:
    quote: OptionQuote
    side: Side
    weight: float = 1.0        # відносна кількість всередині конструкції


@dataclass
class Candidate:
    """Шаблон конструкції без визначених розмірів."""

    name: str
    market: PredictionMarket
    outcome: Outcome
    options: list[OptionSpec] = field(default_factory=list)
    rationale: str = ""
    tags: list[str] = field(default_factory=list)


def _pick_expiry(chain: OptionChain, deadline: dt.datetime) -> tuple[dt.datetime, float]:
    """Найближча експірація НЕ РАНІШЕ дедлайну; якщо немає — найдальша.

    Повертає (expiry, gap_days). gap_days > 0 означає, що опціон гасне
    РАНІШЕ за резолв ставки — це вимагає ролу і окремої лінії ризику.
    """
    exps = chain.expiries()
    if not exps:
        raise ValueError("порожній ланцюг")
    after = [e for e in exps if e >= deadline]
    if after:
        return after[0], 0.0
    last = exps[-1]
    return last, (deadline - last).total_seconds() / 86400.0


def build_candidates(
    market: PredictionMarket,
    chain: OptionChain,
    strike_ratios: Iterable[float] = (0.95, 1.0, 1.05, 1.1, 1.2),
    include_spreads: bool = True,
    max_candidates: int = 60,
    max_expiry_gap_days: float = 30.0,
) -> list[Candidate]:
    """Кандидати для одного ринку.

    Логіка за типом твердження:
      TOUCH_ABOVE  -> ставимо NO (ціна не дійде) і хеджуємо зростання КОЛЛОМ;
      TOUCH_BELOW  -> ставимо NO і хеджуємо падіння ПУТОМ;
      ABOVE_AT_EXPIRY -> те саме, але хедж точніший (digital ≈ call spread).
    """
    claim = market.claim
    expiry, gap = _pick_expiry(chain, claim.deadline)
    if gap > max_expiry_gap_days:
        # Опціон гасне надто рано: конструкція вимагала б ролу, вартість
        # якого сьогодні невідома. Такі пари не пропонуємо як «вилку».
        return []
    quotes = chain.by_expiry(expiry)
    if not quotes:
        return []

    out: list[Candidate] = []
    spot = chain.spot
    tags_base = [f"expiry_gap:{gap:.0f}d"] if gap > 0 else []

    down = claim.kind in (ClaimKind.TOUCH_BELOW, ClaimKind.BELOW_AT_EXPIRY)
    opt_type = OptionType.PUT if down else OptionType.CALL

    seen: set[tuple] = set()
    for ratio in strike_ratios:
        target = (spot if down else spot) * ratio
        q = chain.nearest_strike(expiry, target, opt_type)
        if q is None or (q.ask is None and q.mark is None):
            continue
        key = (q.symbol,)
        if key in seen:
            continue
        seen.add(key)
        out.append(
            Candidate(
                name=f"NO+{opt_type.value}@{q.strike:g}",
                market=market,
                outcome=Outcome.NO,
                options=[OptionSpec(q, Side.BUY, 1.0)],
                rationale=(
                    f"Ставка NO на «{claim.raw_title or claim.kind.value}» + довгий "
                    f"{opt_type.value} {q.strike:g} на {expiry:%Y-%m-%d}"
                ),
                tags=list(tags_base),
            )
        )

        if include_spreads:
            # верхня нога спреду біля бар'єра: дешевше, але з обмеженим верхом
            far = chain.nearest_strike(
                expiry,
                claim.threshold * (0.95 if not down else 1.05),
                opt_type,
            )
            if far is not None and abs(far.strike - q.strike) > 1e-9:
                out.append(
                    Candidate(
                        name=f"NO+{opt_type.value}spread {q.strike:g}/{far.strike:g}",
                        market=market,
                        outcome=Outcome.NO,
                        options=[
                            OptionSpec(q, Side.BUY, 1.0),
                            OptionSpec(far, Side.SELL, 1.0),
                        ],
                        rationale=(
                            "Той самий хедж, але через спред: дешевше на вході, "
                            "стеля прибутку — на страйку продажу"
                        ),
                        tags=list(tags_base) + ["capped_upside"],
                    )
                )
    if claim.kind in (ClaimKind.ABOVE_AT_EXPIRY, ClaimKind.BELOW_AT_EXPIRY):
        # Для термінальної події існує ТОЧНИЙ хедж: цифровий опціон, який
        # реплікується вузьким call-spread'ом навколо порогу. Це найчистіша
        # вилка з усіх — обидві ноги платять за одну й ту саму подію.
        strikes = sorted({q.strike for q in quotes if q.opt_type is opt_type})
        lo = max((k for k in strikes if k <= claim.threshold), default=None)
        hi = min((k for k in strikes if k > claim.threshold), default=None)
        if lo is not None and hi is not None and hi > lo:
            q_lo = chain.get(expiry, lo, opt_type)
            q_hi = chain.get(expiry, hi, opt_type)
            if q_lo is not None and q_hi is not None:
                out.insert(
                    0,
                    Candidate(
                        name=f"NO+digital {lo:g}/{hi:g}",
                        market=market,
                        outcome=Outcome.NO,
                        options=[OptionSpec(q_lo, Side.BUY, 1.0), OptionSpec(q_hi, Side.SELL, 1.0)],
                        rationale=(
                            f"Реплікація цифрового опціону навколо порогу {claim.threshold:g}: "
                            f"вузький спред {lo:g}/{hi:g} платить майже рівно те саме, "
                            "що і ставка YES"
                        ),
                        tags=list(tags_base) + ["digital_replication"],
                    ),
                )
    return out[:max_candidates]

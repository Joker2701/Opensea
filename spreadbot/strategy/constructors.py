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
    strike_ratios: Iterable[float] = (0.6, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95, 1.05, 1.1, 1.2, 1.3, 1.4),
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
    tags_base = [f"expiry_gap:{gap:.0f}d"] if gap > 0 else []

    down = claim.kind in (ClaimKind.TOUCH_BELOW, ClaimKind.BELOW_AT_EXPIRY, ClaimKind.RACE_LOWER_FIRST)
    opt_type = OptionType.PUT if down else OptionType.CALL

    # Анкеруємось на ПОРІГ СТАВКИ, а не на спот. Умова B (fork.py) рахує
    # payoff опціона рівно НА ПОРОЗІ — її запас визначається відстанню
    # «поріг мінус страйк», а не відстанню «спот мінус страйк». Коли поріг
    # далеко від спота (типовий touch-ринок), різниця непринципова; коли
    # близько (типовий at-expiry-ринок) — прив'язка до спота підбирає
    # страйки з надто малим запасом за умовою B, і вилки не існує зовсім,
    # хоча вона є — просто на інших страйках.
    threshold = claim.threshold

    seen: set[tuple] = set()
    for ratio in strike_ratios:
        # ratio < 1 -> страйк по «безпечний» бік порогу (потрібний напрямок);
        # ratio > 1 -> по інший бік (умова B там недосяжна, solve() сам відкине)
        target = threshold * ratio if not down else threshold / ratio
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

    # Спред (довгий+короткий опціон) як окрема нога циклу по ratio свідомо
    # прибраний звідси: для нього треба гарантувати правильний порядок
    # довгої/короткої ноги відносно порогу (інакше можна випадково
    # зібрати НЕ той спред), а найкращий можливий спред для термінальних
    # подій і так один — вузький, навколо порогу, він будується нижче.
    if claim.kind in (ClaimKind.ABOVE_AT_EXPIRY, ClaimKind.BELOW_AT_EXPIRY):
        # Для термінальної події існує ТОЧНИЙ хедж: цифровий опціон, який
        # реплікується вузьким call/put-spread'ом навколо порогу. Це
        # найчистіша вилка з усіх — обидві ноги платять за одну й ту саму
        # подію, і, на відміну від touch-ринків, тут немає раннього виходу:
        # опціон і ставка резолвляться в один день, тому коротка нога не
        # несе ризику "дорогого відкупу до експірації" — спред тут
        # БЕЗПЕЧНИЙ і його варто пропонувати, навіть якщо include_spreads
        # вимкнено глобально (це налаштування — про touch-ринки).
        strikes = sorted({q.strike for q in quotes if q.opt_type is opt_type})
        lo = max((k for k in strikes if k <= claim.threshold), default=None)
        hi = min((k for k in strikes if k > claim.threshold), default=None)
        safe_to_spread = include_spreads or gap == 0
        if safe_to_spread and lo is not None and hi is not None and hi > lo:
            q_lo = chain.get(expiry, lo, opt_type)
            q_hi = chain.get(expiry, hi, opt_type)
            if q_lo is not None and q_hi is not None:
                # для колла профіт зростає з ціною -> довга нога на нижчому
                # страйку; для пута — навпаки, довга нога на вищому страйку
                long_q, short_q = (q_lo, q_hi) if opt_type is OptionType.CALL else (q_hi, q_lo)
                out.insert(
                    0,
                    Candidate(
                        name=f"NO+digital {lo:g}/{hi:g}",
                        market=market,
                        outcome=Outcome.NO,
                        options=[
                            OptionSpec(long_q, Side.BUY, 1.0),
                            OptionSpec(short_q, Side.SELL, 1.0),
                        ],
                        rationale=(
                            f"Реплікація цифрового опціону навколо порогу {claim.threshold:g}: "
                            f"вузький спред {lo:g}/{hi:g} платить майже рівно те саме, "
                            "що і ставка YES; безпечний, бо опціон і ставка резолвляться "
                            "в один день"
                        ),
                        tags=list(tags_base) + ["digital_replication"],
                    ),
                )
    return out[:max_candidates]

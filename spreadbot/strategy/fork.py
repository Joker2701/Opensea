"""Дві умови вилки — рівно так, як їх формулює стратегія.

    (A) чистий прибуток ставки покриває максимальний збиток по опціону
        (тобто його вартість — премію);
    (B) збиток по ставці покривається мінімальним профітом від опціону.

«Мінімальний профіт опціону» тут має точний зміст: ставка програє тоді і
тільки тоді, коли ціна дійшла до порогу. Отже мінімальний профіт опціону —
це його профіт САМЕ НА ПОРОЗІ (внутрішня вартість B − K мінус премія).
Вище порогу профіт лише більший, тому це і є мінімум по всій області,
де ставка програла.

Обидві умови перевіряються в доларах, з урахуванням комісій.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..models import ClaimKind, EventClaim, OptionType, Side, Structure


@dataclass
class ForkCheck:
    bet_profit: float          # чистий прибуток ставки, якщо вона виграла
    bet_stake: float           # скільки втрачаємо, якщо ставка програла
    option_premium: float      # чиста сплачена премія (з комісіями)
    option_payoff_at_barrier: float   # виплата опціонних ніг на порозі
    option_profit_at_barrier: float   # вона ж мінус премія
    margin_a: float            # запас за умовою (A), $
    margin_b: float            # запас за умовою (B), $
    barrier: float

    @property
    def ok_a(self) -> bool:
        return self.margin_a >= 0.0

    @property
    def ok_b(self) -> bool:
        return self.margin_b >= 0.0

    @property
    def ok(self) -> bool:
        return self.ok_a and self.ok_b


def check(structure: Structure, claim: EventClaim) -> ForkCheck:
    """Перевірити конструкцію на обидві умови вилки."""
    leg = structure.prediction
    bet_stake = leg.cost
    bet_profit = leg.size - leg.cost          # виплата $1 за контракт мінус вкладене

    premium = sum(l.cost for l in structure.options) + structure.margin

    # Поріг, на якому ставка програє
    barrier = claim.threshold

    payoff = 0.0
    for l in structure.options:
        if l.quote.opt_type is OptionType.CALL:
            intrinsic = max(barrier - l.quote.strike, 0.0)
        else:
            intrinsic = max(l.quote.strike - barrier, 0.0)
        sign = 1.0 if l.side is Side.BUY else -1.0
        payoff += sign * l.qty * intrinsic

    profit_at_barrier = payoff - premium

    return ForkCheck(
        bet_profit=bet_profit,
        bet_stake=bet_stake,
        option_premium=premium,
        option_payoff_at_barrier=payoff,
        option_profit_at_barrier=profit_at_barrier,
        margin_a=bet_profit - premium,
        margin_b=profit_at_barrier - bet_stake,
        barrier=barrier,
    )


def min_ratio_for_condition_b(
    bet_price: float, barrier: float, strike: float, premium_per_unit: float
) -> float:
    """Мінімум одиниць базового активу на $1000 виплати ставки за умовою (B)."""
    width = barrier - strike
    if width <= 0:
        return float("inf")
    return 1000.0 * bet_price / max(width - premium_per_unit, 1e-9)


def max_ratio_for_condition_a(bet_price: float, premium_per_unit: float) -> float:
    """Максимум одиниць на $1000 виплати за умовою (A)."""
    if premium_per_unit <= 0:
        return float("inf")
    return 1000.0 * (1.0 - bet_price) / premium_per_unit

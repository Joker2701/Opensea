"""Комісії, проковзування та реальна ціна виконання.

Едж у 2-3 в.п. з'їдається комісіями і спредом миттєво, тому цей модуль —
не «дрібниця в кінці», а частина ядра: сканер має рахувати ЧИСТИЙ едж.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from ..models import OrderBook, Side


# --------------------------------------------------------------------------- #
@dataclass
class Fill:
    avg_price: float
    size: float
    filled: bool
    worst_price: float


def walk_book(book: OrderBook, size: float, side: Side, limit: Optional[float] = None) -> Fill:
    """Реальне виконання по стакану з обмеженням за ціною."""
    levels = book.asks if side is Side.BUY else book.bids
    left, cost, worst = size, 0.0, 0.0
    for lvl in levels:
        if limit is not None:
            if side is Side.BUY and lvl.price > limit:
                break
            if side is Side.SELL and lvl.price < limit:
                break
        take = min(left, lvl.size)
        if take <= 0:
            continue
        cost += take * lvl.price
        worst = lvl.price
        left -= take
        if left <= 1e-12:
            break
    done = size - left
    if done <= 0:
        return Fill(avg_price=float("nan"), size=0.0, filled=False, worst_price=float("nan"))
    return Fill(avg_price=cost / done, size=done, filled=left <= 1e-9, worst_price=worst)


# --------------------------------------------------------------------------- #
@dataclass
class PredictionFees:
    """Комісії предикт-маркету.

    * Kalshi: fee = ceil(0.07 · contracts · P · (1−P)) — максимальна
      посередині (P=0.5), майже нульова на «хвостах».
    * Polymarket: до 1 квітня 2026 торгової комісії не було. **З 1 квітня
      2026 введено комісію на РИНКОВІ (marketable/taker) ордери** — та сама
      параболічна форма, що й у Kalshi, тільки коефіцієнт дає максимум
      1,8% (замість 7%) при P=0,5, і рахується вона **від кількості
      куплених шейрів**, а не від витрачених доларів — тому в перерахунку
      на вкладені гроші ефективна ставка вища, ніж «1,8%» (у прикладі з
      джерела: $500 на 1000 шейрів по 50% = $18 комісії = 3,6% від суми).
      **Лімітні ордери цієї комісії не платять** — тому в боті вона
      застосовується лише до "маркет" ноги розрахунку (та, що йде в
      гарантію worst>=0, через прохід по стакану), а показана поруч
      "лімітка по топу книги" лишається довідковою і без цієї комісії.
    """

    venue: str = "generic"
    taker_bps: float = 0.0          # від нотіоналу виплати
    #: параболічна комісія a·P·(1−P) (Kalshi: від доларів; Polymarket:
    #: та сама форма, але від кількості шейрів — рахунок однаковий, бо
    #: тут size вже виражений у шейрах = payout $ при виграші)
    parabolic_taker_fee: bool = False
    parabolic_taker_coeff: float = 0.07
    fixed_usd: float = 0.0          # газ/релеєр на угоду
    settlement_bps: float = 0.0     # збір при резолві (Kalshi: 0)

    def entry_fee(self, size: float, price: float) -> float:
        if self.parabolic_taker_fee:
            raw = self.parabolic_taker_coeff * size * price * (1.0 - price)
            return math.ceil(raw * 100.0) / 100.0 + self.fixed_usd
        return size * price * self.taker_bps / 1e4 + self.fixed_usd

    def settle_fee(self, size: float) -> float:
        return size * self.settlement_bps / 1e4


@dataclass
class OptionFees:
    """Комісії крипто-опціонних бірж.

    Типова схема (Deribit / Bybit / OKX): відсоток від НОТІОНАЛУ базового
    активу, але не більше X% від премії — саме тому дешеві OTM-опціони
    фактично платять комісію «за премією», а не «за спотом».
    """

    venue: str = "generic"
    taker_pct_of_underlying: float = 0.0003
    maker_pct_of_underlying: float = 0.0003
    cap_pct_of_premium: float = 0.125
    delivery_pct_of_underlying: float = 0.00015
    delivery_cap_pct_of_settlement: float = 0.125

    def trade_fee(self, qty: float, premium: float, index: float, taker: bool = True) -> float:
        rate = self.taker_pct_of_underlying if taker else self.maker_pct_of_underlying
        raw = qty * index * rate
        cap = qty * premium * self.cap_pct_of_premium
        return min(raw, cap) if premium > 0 else raw

    def delivery_fee(self, qty: float, index: float, settlement_value: float) -> float:
        raw = qty * index * self.delivery_pct_of_underlying
        if settlement_value <= 0:
            return 0.0
        cap = settlement_value * self.delivery_cap_pct_of_settlement
        return min(raw, cap)


DEFAULT_PREDICTION_FEES = {
    # 0,072 · P·(1−P) дає рівно 1,8% при P=0,5 — максимум, заявлений
    # Polymarket з 1 квітня 2026 (джерело: гайд користувача). Діє тільки
    # на маркет-ноги; лімітка — без комісії.
    "polymarket": PredictionFees(
        venue="polymarket", parabolic_taker_fee=True, parabolic_taker_coeff=0.072
    ),
    "kalshi": PredictionFees(venue="kalshi", parabolic_taker_fee=True),
    "limitless": PredictionFees(venue="limitless", taker_bps=0.0),
}

DEFAULT_OPTION_FEES = {
    "deribit": OptionFees(venue="deribit"),
    "bybit": OptionFees(venue="bybit", cap_pct_of_premium=0.10),
    "okx": OptionFees(venue="okx", cap_pct_of_premium=0.125),
}

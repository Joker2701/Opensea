"""Канонічна модель даних.

Усе, що приходить з будь-якої платформи (Polymarket, Kalshi, Deribit, Bybit...),
нормалізується в ці структури. Стратегічний рушій нічого не знає про конкретні
API — він працює лише з цими типами.
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Optional


# --------------------------------------------------------------------------- #
# Базові енумерації
# --------------------------------------------------------------------------- #
class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OptionType(str, Enum):
    CALL = "call"
    PUT = "put"


class ClaimKind(str, Enum):
    """Тип твердження, на яке ставить предикт-маркет.

    Ключова відмінність, яку більшість «на око» ігнорує: TOUCH_* — це
    бар'єрна (path-dependent) подія, а *_AT_EXPIRY — термінальна.
    Ціна за них принципово різна (для touch вона приблизно вдвічі вища).
    """

    TOUCH_ABOVE = "touch_above"        # "чи торкнеться ціна X до дати D"
    TOUCH_BELOW = "touch_below"
    ABOVE_AT_EXPIRY = "above_at_expiry"  # "чи буде ціна > X станом на дату D"
    BELOW_AT_EXPIRY = "below_at_expiry"
    RANGE_AT_EXPIRY = "range_at_expiry"  # "чи буде ціна між X та Y на дату D"


class Outcome(str, Enum):
    YES = "yes"
    NO = "no"


# --------------------------------------------------------------------------- #
# Книга заявок
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Level:
    price: float
    size: float          # у контрактах / шт. payout-одиниць


@dataclass
class OrderBook:
    bids: list[Level] = field(default_factory=list)   # спадаюча ціна
    asks: list[Level] = field(default_factory=list)   # зростаюча ціна
    ts: Optional[dt.datetime] = None

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0].price if self.asks else None

    @property
    def mid(self) -> Optional[float]:
        if self.best_bid is None or self.best_ask is None:
            return self.best_bid if self.best_ask is None else self.best_ask
        return 0.5 * (self.best_bid + self.best_ask)

    def depth_up_to(self, limit_price: float, side: Side) -> float:
        """Скільки можна купити (BUY -> ходимо по asks) не гірше за limit_price."""
        levels = self.asks if side is Side.BUY else self.bids
        total = 0.0
        for lvl in levels:
            ok = lvl.price <= limit_price if side is Side.BUY else lvl.price >= limit_price
            if not ok:
                break
            total += lvl.size
        return total

    def vwap_for_size(self, size: float, side: Side) -> Optional[float]:
        """Середня ціна виконання ринковою заявкою заданого розміру."""
        levels = self.asks if side is Side.BUY else self.bids
        left, cost = size, 0.0
        for lvl in levels:
            take = min(left, lvl.size)
            cost += take * lvl.price
            left -= take
            if left <= 1e-12:
                return cost / size
        return None  # книга тонша за потрібний розмір


# --------------------------------------------------------------------------- #
# Предикт-маркет
# --------------------------------------------------------------------------- #
@dataclass
class EventClaim:
    """Нормалізоване твердження ринку, незалежне від майданчика."""

    asset: str                     # "ETH", "BTC", ...
    kind: ClaimKind
    threshold: float
    deadline: dt.datetime          # UTC, момент резолву
    window_start: Optional[dt.datetime] = None   # для TOUCH: з якого моменту рахують
    threshold_hi: Optional[float] = None         # для RANGE
    resolution_source: str = "unknown"           # "binance_1m_high", "coinbase_close", ...
    raw_title: str = ""

    def days_to_deadline(self, now: Optional[dt.datetime] = None) -> float:
        now = now or dt.datetime.now(dt.timezone.utc)
        return max((self.deadline - now).total_seconds() / 86400.0, 0.0)

    def years_to_deadline(self, now: Optional[dt.datetime] = None) -> float:
        return self.days_to_deadline(now) / 365.0

    @property
    def is_touch(self) -> bool:
        return self.kind in (ClaimKind.TOUCH_ABOVE, ClaimKind.TOUCH_BELOW)

    def resolves_yes(self, path_max: float, path_min: float, s_final: float) -> bool:
        """Чи резолвиться YES для заданої траєкторії (max/min/фінал)."""
        if self.kind is ClaimKind.TOUCH_ABOVE:
            return path_max >= self.threshold
        if self.kind is ClaimKind.TOUCH_BELOW:
            return path_min <= self.threshold
        if self.kind is ClaimKind.ABOVE_AT_EXPIRY:
            return s_final >= self.threshold
        if self.kind is ClaimKind.BELOW_AT_EXPIRY:
            return s_final <= self.threshold
        if self.kind is ClaimKind.RANGE_AT_EXPIRY:
            hi = self.threshold_hi if self.threshold_hi is not None else math.inf
            return self.threshold <= s_final <= hi
        raise ValueError(self.kind)


@dataclass
class PredictionMarket:
    venue: str                     # "polymarket", "kalshi", "limitless"
    market_id: str
    claim: EventClaim
    yes_book: OrderBook = field(default_factory=OrderBook)
    no_book: OrderBook = field(default_factory=OrderBook)
    volume_usd: float = 0.0
    open_interest_usd: float = 0.0
    fetched_at: Optional[dt.datetime] = None
    url: str = ""
    raw: dict = field(default_factory=dict)

    def book(self, outcome: Outcome) -> OrderBook:
        return self.yes_book if outcome is Outcome.YES else self.no_book

    @property
    def implied_yes(self) -> Optional[float]:
        """Ринкова ймовірність YES (мід YES-книги, з фолбеком на NO)."""
        m = self.yes_book.mid
        if m is not None:
            return m
        m = self.no_book.mid
        return None if m is None else 1.0 - m


# --------------------------------------------------------------------------- #
# Опціони
# --------------------------------------------------------------------------- #
@dataclass
class OptionQuote:
    venue: str
    symbol: str
    underlying: str
    expiry: dt.datetime
    strike: float
    opt_type: OptionType
    bid: Optional[float] = None          # премія В USD за 1 одиницю базового активу
    ask: Optional[float] = None
    mark: Optional[float] = None
    iv_bid: Optional[float] = None
    iv_ask: Optional[float] = None
    iv_mark: Optional[float] = None
    delta: Optional[float] = None
    open_interest: float = 0.0
    contract_size: float = 1.0           # скільки базового активу в 1 контракті
    min_qty: float = 0.1                 # мінімальний крок кількості
    book: OrderBook = field(default_factory=OrderBook)

    def years_to_expiry(self, now: Optional[dt.datetime] = None) -> float:
        now = now or dt.datetime.now(dt.timezone.utc)
        return max((self.expiry - now).total_seconds() / (365 * 86400.0), 1e-6)


@dataclass
class OptionChain:
    venue: str
    underlying: str
    spot: float
    fetched_at: dt.datetime
    quotes: list[OptionQuote] = field(default_factory=list)
    forwards: dict[dt.datetime, float] = field(default_factory=dict)  # expiry -> F
    risk_free: float = 0.0

    def expiries(self) -> list[dt.datetime]:
        return sorted({q.expiry for q in self.quotes})

    def by_expiry(self, expiry: dt.datetime) -> list[OptionQuote]:
        return sorted(
            (q for q in self.quotes if q.expiry == expiry), key=lambda q: q.strike
        )

    def get(
        self, expiry: dt.datetime, strike: float, opt_type: OptionType
    ) -> Optional[OptionQuote]:
        for q in self.quotes:
            if q.expiry == expiry and abs(q.strike - strike) < 1e-9 and q.opt_type is opt_type:
                return q
        return None

    def forward(self, expiry: dt.datetime) -> float:
        return self.forwards.get(expiry, self.spot)

    def nearest_strike(
        self, expiry: dt.datetime, target: float, opt_type: OptionType
    ) -> Optional[OptionQuote]:
        cands = [q for q in self.by_expiry(expiry) if q.opt_type is opt_type]
        return min(cands, key=lambda q: abs(q.strike - target), default=None)


# --------------------------------------------------------------------------- #
# Конструкція угоди
# --------------------------------------------------------------------------- #
@dataclass
class PredictionLeg:
    market: PredictionMarket
    outcome: Outcome
    side: Side                 # практично завжди BUY
    size: float                # кількість контрактів = payout у $ при виграші
    price: float               # ціна виконання з урахуванням проковзування (0..1)
    fee: float = 0.0           # $ разом

    @property
    def cost(self) -> float:
        return self.size * self.price + self.fee

    def payoff(self, yes_wins: bool) -> float:
        """Валовий payout (без віднімання cost)."""
        won = yes_wins if self.outcome is Outcome.YES else not yes_wins
        return self.size if won else 0.0


@dataclass
class OptionLeg:
    quote: OptionQuote
    side: Side
    qty: float                 # у одиницях базового активу
    price: float               # премія за одиницю, USD, з проковзуванням
    fee: float = 0.0

    @property
    def cost(self) -> float:
        sign = 1.0 if self.side is Side.BUY else -1.0
        return sign * self.qty * self.price + self.fee

    def payoff(self, s_final: float) -> float:
        intrinsic = (
            max(s_final - self.quote.strike, 0.0)
            if self.quote.opt_type is OptionType.CALL
            else max(self.quote.strike - s_final, 0.0)
        )
        sign = 1.0 if self.side is Side.BUY else -1.0
        return sign * self.qty * intrinsic


@dataclass
class Structure:
    """Повна конструкція: нога предикт-маркету + одна або кілька опціонних ніг."""

    name: str
    prediction: PredictionLeg
    options: list[OptionLeg] = field(default_factory=list)
    note: str = ""
    #: маржа під непокриті короткі ноги (для дебетових спредів = 0)
    margin: float = 0.0

    @property
    def capital(self) -> float:
        """Реально задіяні гроші: ставка + ЧИСТА премія (кредит від коротких
        ніг зменшує вкладення) + маржа під непокриті шорти."""
        return self.prediction.cost + sum(l.cost for l in self.options) + self.margin

    def terminal_pnl(self, yes_wins: bool, s_final: float) -> float:
        pnl = self.prediction.payoff(yes_wins) - self.prediction.cost
        for leg in self.options:
            pnl += leg.payoff(s_final) - leg.cost
        return pnl


@dataclass
class Scenario:
    label: str
    yes_wins: bool
    s_final: float
    prob: float = 0.0
    #: для політики «розхедж при торканні» — момент торкання, роки від сьогодні
    tau_years: Optional[float] = None
    unwind: bool = False


@dataclass
class Metrics:
    capital: float = 0.0
    worst_pnl: float = 0.0
    worst_scenario: str = ""
    best_pnl: float = 0.0
    ev_pnl: float = 0.0
    prob_profit: float = 0.0
    worst_return: float = 0.0
    cvar_pnl: float = 0.0          # середній P&L у найгірших alpha% сценаріїв
    cvar_return: float = 0.0
    prob_loss: float = 0.0
    fork_ok: bool = False          # обидві умови вилки виконані
    margin_a: float = 0.0          # запас за умовою (A): прибуток ставки − премія
    margin_b: float = 0.0          # запас за умовою (B): профіт опціона на порозі − ставка
    bet_win_floor: float = 0.0     # найгірший P&L серед сценаріїв, де СТАВКА ВИГРАЛА
    bet_lose_floor: float = 0.0    # найгірший P&L серед сценаріїв, де ставка програла
    ev_return: float = 0.0
    ev_apr: float = 0.0
    edge_pp: float = 0.0           # (ринкова ймовірн. - модельна) * 100
    model_prob: float = 0.0
    market_prob: float = 0.0
    days: float = 0.0
    breakeven_up: Optional[float] = None
    liquidity_usd: float = 0.0
    flags: list[str] = field(default_factory=list)


@dataclass
class Opportunity:
    structure: Structure
    metrics: Metrics
    scenarios: list[Scenario] = field(default_factory=list)
    score: float = 0.0
    created_at: dt.datetime = field(
        default_factory=lambda: dt.datetime.now(dt.timezone.utc)
    )

    @property
    def key(self) -> str:
        pm = self.structure.prediction.market
        legs = "+".join(l.quote.symbol for l in self.structure.options)
        return f"{pm.venue}:{pm.market_id}:{self.structure.prediction.outcome.value}|{legs}"

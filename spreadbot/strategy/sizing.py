"""Пошук розмірів, за яких вилка справді існує.

Дві умови з «народної» версії стратегії:
   (A) чистий прибуток ставки >= максимальний збиток по опціону (премія);
   (B) збиток по ставці <= прибуток по опціону у сценарії програшу ставки.

Умова (B) в оригіналі сформульована некоректно: «мінімальний прибуток
опціону» не визначений, бо європейський опціон може коштувати НУЛЬ навіть
після торкання бар'єра. Ми замінюємо її на явний сценарний бюджет ризику:
    worst_case P&L по ВСЬОМУ спільному розподілу >= -max_loss_frac * капітал.
"""
from __future__ import annotations

import datetime as dt
import logging
import math
from dataclasses import dataclass, field
from typing import Optional

from ..models import (
    Metrics,
    OptionLeg,
    Outcome,
    PredictionLeg,
    Scenario,
    Side,
    Structure,
)
from .constructors import Candidate
from .costs import OptionFees, PredictionFees, walk_book
from .fork import check as fork_check
from .payoff import evaluate

log = logging.getLogger(__name__)


@dataclass
class SizingConfig:
    capital_usd: float = 10_000.0
    #: чим міряємо ризик: "cvar" (середнє по найгіршому хвості) або "worst"
    risk_measure: str = "cvar"
    cvar_alpha: float = 0.05
    max_loss_frac: float = 0.35        # ліміт на обрану міру ризику (частка капіталу)
    min_worst_return: Optional[float] = None  # жорсткий поріг на найгірший сценарій
    #: головний критерій: обидві умови вилки (див. strategy/fork.py)
    require_fork: bool = True
    #: додаткова перевірка на всьому сценарному розподілі: якщо ставка
    #: виграла — конструкція не в мінусі
    require_bet_win_breakeven: bool = True
    bet_win_floor_frac: float = 0.0    # дозволений мінус у сценарії виграшу ставки
    objective: str = "ev"              # "ev" | "maximin" | "cvar"
    #: як оцінювати сценарій «ставка програла»:
    #: "unwind" — опціон продається в момент, коли ціна дійшла до порогу
    #: (відповідає умові B), "hold" — доживає до експірації
    exit_policy: str = "unwind"
    unwind_cost_frac: float = 0.04     # спред+комісія при достроковому виході
    ratio_min: float = 0.0
    ratio_max: float = 5.0             # одиниць базового активу на $1000 виплати ставки
    ratio_steps: int = 60
    pm_limit_price: Optional[float] = None    # ліміт ціни входу в ставку (0..1)
    option_slippage_bps: float = 0.0   # додатковий буфер, якщо стакан невідомий
    min_capital_usd: float = 100.0
    require_full_fill: bool = True


UNIT_PAYOUT = 1000.0  # одиниця масштабу: $1000 виплати по ставці


def _pm_fill_price(cand: Candidate, cfg: SizingConfig, size: float) -> Optional[tuple[float, float]]:
    book = cand.market.book(cand.outcome)
    fill = walk_book(book, size, Side.BUY, cfg.pm_limit_price)
    if fill.size <= 0:
        return None
    # часткове виконання допустиме: розмір ставки просто обмежується глибиною
    return fill.avg_price, fill.size


def _opt_fill_price(spec_quote, side: Side, qty: float, cfg: SizingConfig) -> Optional[float]:
    book = spec_quote.book
    if book.asks or book.bids:
        fill = walk_book(book, qty, side)
        if fill.size > 0 and (fill.filled or not cfg.require_full_fill):
            return fill.avg_price
    px = spec_quote.ask if side is Side.BUY else spec_quote.bid
    if px is None:
        px = spec_quote.mark
    if px is None:
        return None
    bump = 1.0 + (cfg.option_slippage_bps / 1e4) * (1.0 if side is Side.BUY else -1.0)
    return px * bump


def build_structure(
    cand: Candidate,
    ratio: float,
    pm_payout: float,
    spot: float,
    cfg: SizingConfig,
    pm_fees: PredictionFees,
    opt_fees: OptionFees,
    round_lots: bool = True,
) -> Optional[Structure]:
    """Зібрати конструкцію із заданим співвідношенням ніг і масштабом."""
    if round_lots and cand.options:
        # Лотність опціонів (Deribit: 1 ETH, Bybit: 0.1 ETH) — головне
        # обмеження реального розміру. Замість того щоб «зрізати» кількість
        # опціонів і зіпсувати співвідношення ніг, ПІДГАНЯЄМО розмір ставки
        # під ціле число контрактів: пропорція лишається точною.
        ref = cand.options[0]
        step = max(ref.quote.min_qty, 1e-9)
        qty_ref = math.floor(ratio * ref.weight * pm_payout / UNIT_PAYOUT / step + 1e-9) * step
        if qty_ref <= 0:
            return None
        pm_payout = qty_ref * UNIT_PAYOUT / (ratio * ref.weight)

    filled = _pm_fill_price(cand, cfg, pm_payout)
    if filled is None:
        return None
    pm_price, pm_size = filled
    if pm_size <= 0 or pm_price <= 0 or pm_price >= 1:
        return None

    pm_leg = PredictionLeg(
        market=cand.market,
        outcome=cand.outcome,
        side=Side.BUY,
        size=pm_size,
        price=pm_price,
        fee=pm_fees.entry_fee(pm_size, pm_price),
    )

    legs: list[OptionLeg] = []
    for spec in cand.options:
        qty_raw = ratio * spec.weight * pm_size / UNIT_PAYOUT
        if round_lots:
            step = max(spec.quote.min_qty, 1e-9)
            qty = math.floor(qty_raw / step + 1e-9) * step
        else:
            qty = qty_raw          # пошук співвідношення — без лотності
        if qty <= 0:
            return None
        px = _opt_fill_price(spec.quote, spec.side, qty, cfg)
        if px is None or px <= 0:
            return None
        legs.append(
            OptionLeg(
                quote=spec.quote,
                side=spec.side,
                qty=qty,
                price=px,
                fee=opt_fees.trade_fee(qty, px, spot, taker=True),
            )
        )

    return Structure(
        name=cand.name,
        prediction=pm_leg,
        options=legs,
        note=cand.rationale,
        margin=required_margin(legs, spot),
    )


def required_margin(legs: list[OptionLeg], spot: float, uncovered_frac: float = 0.15) -> float:
    """Маржа під короткі опціонні ноги.

    Дебетовий спред (довгий нижчий страйк + короткий вищий, та сама
    експірація і кількість) має обмежений збиток і додаткової маржі не
    потребує. Непокритий шорт — потребує, і це принципово міняє капітал,
    тому такі конструкції ми за замовчуванням і не будуємо.
    """
    margin = 0.0
    for short in legs:
        if short.side is not Side.SELL:
            continue
        covered = any(
            long.side is Side.BUY
            and long.quote.expiry == short.quote.expiry
            and long.quote.opt_type is short.quote.opt_type
            and long.qty >= short.qty
            and (
                long.quote.strike <= short.quote.strike
                if short.quote.opt_type.value == "call"
                else long.quote.strike >= short.quote.strike
            )
            for long in legs
        )
        if not covered:
            margin += uncovered_frac * spot * short.qty
    return margin


@dataclass
class SizingResult:
    structure: Structure
    metrics: Metrics
    ratio: float
    scale: float
    rejected: list[str] = field(default_factory=list)


def solve(
    cand: Candidate,
    scenarios: list[Scenario],
    spot: float,
    days: float,
    cfg: SizingConfig,
    pm_fees: PredictionFees,
    opt_fees: OptionFees,
    market_prob: float = 0.0,
    model_prob: float = 0.0,
    pnl_fn=None,
) -> Optional[SizingResult]:
    """Двопрохідний пошук: спершу співвідношення ніг, потім реальний масштаб.

    P&L однорідний за масштабом (з точністю до проковзування і лотності),
    тому спочатку шукаємо оптимальний RATIO на одиничному масштабі,
    а вже потім розтягуємо позицію під капітал і глибину стаканів.
    """
    best: Optional[SizingResult] = None
    rejects: list[str] = []   # діагностика: чому відкинули те чи інше співвідношення

    def risk_of(m: Metrics) -> float:
        """Обрана міра ризику як ЧАСТКА капіталу (додатне число = збиток)."""
        return -(m.cvar_return if cfg.risk_measure == "cvar" else m.worst_return)

    def objective_of(m: Metrics) -> float:
        if cfg.objective == "maximin":
            return m.worst_pnl
        if cfg.objective == "cvar":
            return m.cvar_pnl
        return m.ev_pnl

    def check_fork(st: Structure, m: Metrics) -> Optional[str]:
        """Дві умови стратегії — головний фільтр, рахується в доларах."""
        fc = fork_check(st, cand.market.claim)
        m.fork_ok, m.margin_a, m.margin_b = fc.ok, fc.margin_a, fc.margin_b
        if not cfg.require_fork:
            return None
        if not fc.ok_a:
            return f"умова A не виконана: прибуток ставки {fc.bet_profit:,.0f} < премії {fc.option_premium:,.0f}"
        if not fc.ok_b:
            return (
                f"умова B не виконана: профіт опціона на порозі "
                f"{fc.option_profit_at_barrier:,.0f} < ставки {fc.bet_stake:,.0f}"
            )
        return None

    def passes(m: Metrics) -> Optional[str]:
        if cfg.max_loss_frac > 0 and risk_of(m) > cfg.max_loss_frac:
            return f"ризик {risk_of(m):.1%} > ліміту {cfg.max_loss_frac:.1%}"
        if cfg.min_worst_return is not None and m.worst_return < cfg.min_worst_return:
            return f"найгірше {m.worst_return:+.1%} < {cfg.min_worst_return:+.1%}"
        if cfg.require_bet_win_breakeven and m.capital > 0:
            floor = m.bet_win_floor / m.capital
            if floor < -abs(cfg.bet_win_floor_frac) - 1e-9:
                return f"ставка виграла, але P&L {floor:+.1%} < 0 (премія не покрита)"
        return None

    # --- прохід 1: співвідношення ніг, без лотності і без обмежень глибини ---
    for i in range(cfg.ratio_steps + 1):
        ratio = cfg.ratio_min + (cfg.ratio_max - cfg.ratio_min) * i / cfg.ratio_steps
        if ratio <= 0:
            continue
        st = build_structure(
            cand, ratio, UNIT_PAYOUT, spot, cfg, pm_fees, opt_fees, round_lots=False
        )
        if st is None:
            continue
        m = evaluate(st, scenarios, days, market_prob, model_prob, cfg.cvar_alpha, pnl_fn)
        why = check_fork(st, m) or passes(m)
        if why:
            rejects.append(f"ratio={ratio:.3f}: {why}")
            continue
        if best is None or objective_of(m) > objective_of(best.metrics):
            best = SizingResult(structure=st, metrics=m, ratio=ratio, scale=1.0)

    if best is None:
        log.debug("кандидат %s відкинуто: %s", cand.name, "; ".join(rejects[:3]))
        return None

    # --- прохід 2: реальний масштаб під капітал, лотність і глибину стаканів ---
    unit_cap = best.structure.capital
    if unit_cap <= 0:
        return None
    scale = cfg.capital_usd / unit_cap
    scaled = build_structure(
        cand, best.ratio, UNIT_PAYOUT * scale, spot, cfg, pm_fees, opt_fees, round_lots=True
    )
    if scaled is None or scaled.capital < cfg.min_capital_usd:
        return None
    m = evaluate(scaled, scenarios, days, market_prob, model_prob, cfg.cvar_alpha, pnl_fn)
    why = check_fork(scaled, m) or passes(m)
    if why:
        # лотність/проковзування зіпсували вилку на реальному розмірі —
        # це не «майже підходить», це відмова: торгувати нема чого
        log.debug("кандидат %s не пережив масштабування: %s", cand.name, why)
        return None
    m.liquidity_usd = scaled.prediction.size * scaled.prediction.price
    return SizingResult(structure=scaled, metrics=m, ratio=best.ratio, scale=scale, rejected=rejects[:5])

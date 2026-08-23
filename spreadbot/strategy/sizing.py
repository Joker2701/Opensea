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
    #: справжній жорсткий гейт: найгірший сценарій по всьому розподілу
    #: (з комісіями входу і виходу) не може бути гіршим за це значення.
    #: 0.0 = ніколи в мінус; None = вимкнено (не рекомендується)
    min_worst_return: Optional[float] = 0.0
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
    """Пошук у три кроки: вікно на малому масштабі -> те саме вікно на
    реальному масштабі (з реальним проковзуванням) -> вирівнювання по лоту.

    Найгірший сценарій — жорсткий гейт (`cfg.min_worst_return`, за
    замовчуванням 0.0: результат ніколи не йде в мінус). Вікно
    співвідношень, де це тримається, зазвичай вузьке (воно затиснуте
    умовами A і B strategy/fork.py з двох боків) і залежить від розміру
    заявки через проковзування по стакану — тому шукається саме на
    реальному розмірі, а не екстраполюється з малого.
    """
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
        if cfg.min_worst_return is not None and m.worst_return < cfg.min_worst_return - 1e-9:
            return f"найгірше {m.worst_return:+.2%} < {cfg.min_worst_return:+.2%}"
        if cfg.require_bet_win_breakeven and m.capital > 0:
            floor = m.bet_win_floor / m.capital
            if floor < -abs(cfg.bet_win_floor_frac) - 1e-9:
                return f"ставка виграла, але P&L {floor:+.1%} < 0 (премія не покрита)"
        return None

    def at(ratio: float, payout: float) -> tuple[Optional[Structure], Optional[Metrics]]:
        st = build_structure(cand, ratio, payout, spot, cfg, pm_fees, opt_fees, round_lots=False)
        if st is None:
            return None, None
        return st, evaluate(st, scenarios, days, market_prob, model_prob, cfg.cvar_alpha, pnl_fn)

    floor = cfg.min_worst_return if cfg.min_worst_return is not None else -math.inf

    def find_window(payout: float) -> Optional[SizingResult]:
        """Знайти найкраще (за objective) співвідношення ніг, у якого
        найгірший сценарій НЕ ГІРШИЙ за floor, — на ЗАДАНОМУ масштабі.

        Проковзування по стакану (і на ставці, і на опціоні) залежить від
        АБСОЛЮТНОГО розміру заявки, тому вікно на payout=1000 і на
        payout=цільовий капітал — це РІЗНІ вікна: більший розмір ковтає
        глибший, гірший за ціною шматок книги. Тому цю функцію викликають
        двічі — спершу на малому payout, щоб оцінити масштаб капіталу,
        потім ще раз на реальному, щоб знайти вікно, яке насправді
        витримає реальне виконання.
        """
        def worst_at(ratio: float) -> float:
            _, m = at(ratio, payout)
            return m.worst_return if m is not None else -math.inf

        lo_r = max(cfg.ratio_min, 1e-4)
        hi_r = cfg.ratio_max
        if hi_r <= lo_r:
            return None

        # вершина: worst_return(ratio) емпірично одновершинна (замало хеджа
        # -> росте збиток від торкання; забагато -> росте непокрита премія)
        a, b = lo_r, hi_r
        for _ in range(60):
            m1 = a + (b - a) / 3.0
            m2 = b - (b - a) / 3.0
            if worst_at(m1) < worst_at(m2):
                a = m1
            else:
                b = m2
        r_peak = 0.5 * (a + b)
        st_peak, m_peak = at(r_peak, payout)
        if st_peak is None or m_peak.worst_return < floor - 1e-9:
            return None

        # межі допустимого вікна навколо вершини (бісекція в обидва боки)
        def edge(inner: float, outer: float) -> float:
            for _ in range(50):
                mid = 0.5 * (inner + outer)
                if worst_at(mid) >= floor - 1e-9:
                    inner = mid
                else:
                    outer = mid
            return inner

        r_lo, r_hi = edge(r_peak, lo_r), edge(r_peak, hi_r)

        # усередині вікна максимізуємо цільову метрику (EV за замовчуванням)
        best_here: Optional[SizingResult] = None
        steps = max(cfg.ratio_steps, 20)
        for i in range(steps + 1):
            ratio = r_lo + (r_hi - r_lo) * i / steps
            st, m = at(ratio, payout)
            if st is None:
                continue
            why = check_fork(st, m) or passes(m)
            if why:
                rejects.append(f"payout={payout:.0f} ratio={ratio:.4f}: {why}")
                continue
            if best_here is None or objective_of(m) > objective_of(best_here.metrics):
                best_here = SizingResult(structure=st, metrics=m, ratio=ratio,
                                          scale=payout / UNIT_PAYOUT)

        if best_here is None:
            why = check_fork(st_peak, m_peak) or passes(m_peak)
            if why:
                return None
            best_here = SizingResult(structure=st_peak, metrics=m_peak, ratio=r_peak,
                                      scale=payout / UNIT_PAYOUT)
        return best_here

    # --- прохід 1: оцінити масштаб капіталу на малому payout ---
    approx = find_window(UNIT_PAYOUT)
    if approx is None:
        log.debug("кандидат %s: безпечного вікна немає навіть на малому розмірі", cand.name)
        return None
    unit_cap = approx.structure.capital
    if unit_cap <= 0:
        return None

    # --- прохід 2: те саме вікно, але на РЕАЛЬНОМУ розмірі (з реальним
    # проковзуванням) — саме воно й піде в остаточну структуру ---
    target_payout = UNIT_PAYOUT * cfg.capital_usd / unit_cap
    best = find_window(target_payout)
    if best is None:
        log.debug("кандидат %s: вікно є на малому розмірі, але зникає на реальному "
                   "(проковзування на глибині книги з'їдає запас)", cand.name)
        return None
    if best.structure.capital < cfg.min_capital_usd:
        return None

    # --- прохід 3: лотність, БЕЗ зміни ratio ---
    #
    # Розмір ставки — суцільна величина (не лотована), тому для БУДЬ-ЯКОГО
    # цілого числа лотів опціона можна підібрати такий розмір ставки, що
    # ratio = qty_опціона / payout_ставки лишиться РІВНО тим, що знайдено
    # вище (і, отже, лишиться всередині безпечного вікна). Округлювати сам
    # ratio — от де ламалась гарантія: найближчий лот міг лежати ЗА межами
    # вузького вікна. Тут натомість варіюється лише МАСШТАБ (скільки лотів).
    if not cand.options:
        return None
    ref = cand.options[0]
    step = max(ref.quote.min_qty, 1e-9)
    n_est = max(round(best.ratio * ref.weight * target_payout / (UNIT_PAYOUT * step)), 1)

    final: Optional[SizingResult] = None
    for n in {max(n_est - 1, 1), n_est, n_est + 1}:
        payout = n * step * UNIT_PAYOUT / (ref.weight * best.ratio)
        scaled = build_structure(
            cand, best.ratio, payout, spot, cfg, pm_fees, opt_fees, round_lots=False
        )
        if scaled is None or scaled.capital < cfg.min_capital_usd:
            continue
        m = evaluate(scaled, scenarios, days, market_prob, model_prob, cfg.cvar_alpha, pnl_fn)
        why = check_fork(scaled, m) or passes(m)
        if why:
            # книга виконала гірше за очікуване і зсунула ratio — крайовий
            # випадок; пробуємо інші n, а не здаємось одразу
            rejects.append(f"n={n} лотів: {why}")
            continue
        if final is None or abs(scaled.capital - cfg.capital_usd) < abs(
            final.structure.capital - cfg.capital_usd
        ):
            m.liquidity_usd = scaled.prediction.size * scaled.prediction.price
            final = SizingResult(structure=scaled, metrics=m, ratio=best.ratio,
                                  scale=payout / UNIT_PAYOUT)

    if final is None:
        log.debug("кандидат %s: жоден лот не пройшов після масштабування: %s",
                   cand.name, "; ".join(rejects[-3:]))
        return None
    return final

"""Сценарний рушій: спільний розподіл (результат ставки × ціна на експірації)
і переоцінка позиції в будь-який момент життя (mark-to-market).

Дві ноги конструкції прив'язані до однієї ціни, але спрацьовують у різні
моменти: ставка на торкання вирішується В МОМЕНТ дотику порогу, опціон
живе до експірації. Тому ми будуємо СПІЛЬНИЙ розподіл P(торкання, S_T) і
розподіл ЧАСУ торкання — на них і рахується P&L, включно з умовою (B).
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from typing import Callable, Optional

from ..models import (
    ClaimKind,
    EventClaim,
    Metrics,
    Scenario,
    Structure,
)
from ..pricing.bs import black76
from ..pricing.digital import (
    digital_call,
    prob_touch,
    prob_touch_and_finish_below,
    touch_time_buckets,
)
from ..pricing.surface import VolSurface


# --------------------------------------------------------------------------- #
def terminal_grid(spot: float, lo_mult: float, hi_mult: float, n: int) -> list[float]:
    """Логарифмічно рівномірна сітка цін на експірації."""
    lo, hi = math.log(spot * lo_mult), math.log(spot * hi_mult)
    step = (hi - lo) / n
    return [math.exp(lo + step * (i + 0.5)) for i in range(n)]


def build_scenarios(
    claim: EventClaim,
    surface: VolSurface,
    now: Optional[dt.datetime] = None,
    n: int = 60,
    lo_mult: float = 0.2,
    hi_mult: float = 4.0,
    horizon_years: Optional[float] = None,
    policy: str = "hold",
    n_touch_times: int = 16,
) -> list[Scenario]:
    """Спільний розподіл (yes_wins, S_T) під ризик-нейтральною мірою.

    Для TOUCH_ABOVE:
        P(touch & S_T <= x) рахується формулою відображення;
        P(S_T >= x) — цифровим коллом зі скью.
    Ймовірності кожної комірки нормуються (сума = 1), бо дві формули
    використовують трохи різні sigma (на бар'єрі vs на страйку).
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    t = horizon_years if horizon_years is not None else max(claim.years_to_deadline(now), 1e-6)
    f = surface.forward(t)
    spot = surface.spot
    mu = math.log(f / spot) / t if t > 0 else 0.0

    lo, hi = math.log(spot * lo_mult), math.log(spot * hi_mult)
    step = (hi - lo) / n
    edges = [math.exp(lo + step * i) for i in range(n + 1)]

    def q_above(x: float) -> float:
        """P(S_T >= x) з поверхні."""
        sig = surface.iv(x, t)
        return digital_call(f, x, t, sig, 1.0, surface.skew(x, t))

    b = claim.threshold
    sig_b = surface.iv(b, t)

    scenarios: list[Scenario] = []
    for i in range(n):
        e_lo, e_hi = edges[i], edges[i + 1]
        s_mid = math.sqrt(e_lo * e_hi)
        cell = max(q_above(e_lo) - q_above(e_hi), 0.0)
        if cell <= 0:
            continue

        if claim.kind is ClaimKind.TOUCH_ABOVE:
            if e_lo >= b:
                p_yes = cell           # вище бар'єра -> торкання гарантоване
            elif e_hi <= b:
                p_yes = max(
                    min(
                        prob_touch_and_finish_below(spot, b, e_hi, t, sig_b, mu)
                        - prob_touch_and_finish_below(spot, b, e_lo, t, sig_b, mu),
                        cell,
                    ),
                    0.0,
                )
            else:
                p_yes = cell * 0.5     # комірка перетинає бар'єр
        elif claim.kind is ClaimKind.TOUCH_BELOW:
            if e_hi <= b:
                p_yes = cell
            elif e_lo >= b:
                # симетрично: P(торкнувся знизу та S_T >= x)
                p_yes = max(
                    min(
                        _touch_below_and_finish_above(spot, b, e_lo, t, sig_b, mu)
                        - _touch_below_and_finish_above(spot, b, e_hi, t, sig_b, mu),
                        cell,
                    ),
                    0.0,
                )
            else:
                p_yes = cell * 0.5
        else:
            p_yes = cell if claim.resolves_yes(s_mid, s_mid, s_mid) else 0.0

        p_no = max(cell - p_yes, 0.0)
        if p_yes > 1e-9:
            scenarios.append(Scenario(f"YES @ S_T≈{s_mid:,.0f}", True, s_mid, p_yes))
        if p_no > 1e-9:
            scenarios.append(Scenario(f"NO  @ S_T≈{s_mid:,.0f}", False, s_mid, p_no))

    if policy == "unwind" and claim.is_touch:
        # Гілку «ставка програла» замінюємо на розподіл ЧАСУ ПЕРШОГО ТОРКАННЯ:
        # у цей момент позиція розхеджується (опціон продається живим,
        # з часовою вартістю), а не доживає до експірації порожньою.
        no_touch = [sc for sc in scenarios if not sc.yes_wins]
        buckets = touch_time_buckets(spot, b, t, sig_b, mu, n_touch_times)
        scenarios = no_touch + [
            Scenario(
                label=f"TOUCH через {tau*365:.0f}д",
                yes_wins=True,
                s_final=b,
                prob=p,
                tau_years=tau,
                unwind=True,
            )
            for tau, p in buckets
            if p > 1e-9
        ]

    total = sum(s.prob for s in scenarios)
    if total > 0:
        for s in scenarios:
            s.prob /= total
    return scenarios


def _touch_below_and_finish_above(
    s: float, b: float, k: float, t: float, sigma: float, mu: float
) -> float:
    """P(min <= B та S_T >= K) для B < S, K >= B — дзеркало reflection-формули."""
    if t <= 0 or sigma <= 0:
        return 0.0
    nu = mu - 0.5 * sigma * sigma
    vol = sigma * math.sqrt(t)
    m = math.log(b / s)      # < 0
    x = math.log(k / s)
    z = (2.0 * m - x + nu * t) / vol
    from ..pricing.bs import norm_cdf

    return math.exp(2.0 * nu * m / (sigma * sigma)) * norm_cdf(z)


# --------------------------------------------------------------------------- #
def evaluate(
    structure: Structure,
    scenarios: list[Scenario],
    days: float,
    market_prob: float = 0.0,
    model_prob: float = 0.0,
    cvar_alpha: float = 0.05,
    pnl_fn: Optional[Callable[[Structure, Scenario], float]] = None,
) -> Metrics:
    """Метрики конструкції на спільному розподілі сценаріїв.

    Крім «найгіршого випадку» рахуємо CVaR (середній збиток у найгірших
    alpha% сценаріїв) — бо в такій стратегії найгірший випадок майже завжди
    «мінус увесь капітал», і сам по собі він нічого не ранжує. А також дві
    «структурні підлоги», якими міряють вилку вручну:
        bet_win_floor  — P&L, якщо ставка виграла (має бути >= 0: прибуток
                         ставки мусить покривати премію опціону);
        bet_lose_floor — P&L, якщо ставка програла (тут і живе реальний ризик).
    """
    cap = structure.capital
    worst, worst_label = math.inf, ""
    best = -math.inf
    ev = 0.0
    p_profit = 0.0
    p_loss = 0.0
    win_floor, lose_floor = math.inf, math.inf
    rows: list[tuple[float, float]] = []

    pnl_of = pnl_fn or (lambda st, sc: st.terminal_pnl(sc.yes_wins, sc.s_final))
    for sc in scenarios:
        pnl = pnl_of(structure, sc)
        ev += sc.prob * pnl
        rows.append((pnl, sc.prob))
        if pnl < worst:
            worst, worst_label = pnl, sc.label
        best = max(best, pnl)
        if pnl > 0:
            p_profit += sc.prob
        elif pnl < 0:
            p_loss += sc.prob
        bet_won = (not sc.yes_wins) if structure.prediction.outcome.value == "no" else sc.yes_wins
        if bet_won:
            win_floor = min(win_floor, pnl)
        else:
            lose_floor = min(lose_floor, pnl)

    # CVaR: середній P&L у найгіршому хвості ймовірнісної маси alpha
    rows.sort(key=lambda r: r[0])
    acc, tail_pnl = 0.0, 0.0
    for pnl, prob in rows:
        take = min(prob, cvar_alpha - acc)
        if take <= 0:
            break
        tail_pnl += take * pnl
        acc += take
    cvar = tail_pnl / acc if acc > 0 else (worst if worst is not math.inf else 0.0)

    years = max(days / 365.0, 1e-9)
    ev_ret = ev / cap if cap > 0 else 0.0
    return Metrics(
        capital=cap,
        worst_pnl=worst if worst is not math.inf else 0.0,
        worst_scenario=worst_label,
        best_pnl=best if best is not -math.inf else 0.0,
        ev_pnl=ev,
        prob_profit=p_profit,
        prob_loss=p_loss,
        worst_return=(worst / cap) if cap > 0 else 0.0,
        cvar_pnl=cvar,
        cvar_return=(cvar / cap) if cap > 0 else 0.0,
        bet_win_floor=win_floor if win_floor is not math.inf else 0.0,
        bet_lose_floor=lose_floor if lose_floor is not math.inf else 0.0,
        ev_return=ev_ret,
        ev_apr=((1.0 + ev_ret) ** (1.0 / years) - 1.0) if ev_ret > -1 else -1.0,
        edge_pp=(market_prob - model_prob) * 100.0,
        model_prob=model_prob,
        market_prob=market_prob,
        days=days,
    )


# --------------------------------------------------------------------------- #
@dataclass
class MtmPoint:
    days_from_now: float
    spot: float
    value: float
    pnl: float
    touched: bool


def mark_to_market(
    structure: Structure,
    claim: EventClaim,
    surface: VolSurface,
    days_from_now: float,
    spot: float,
    touched: bool = False,
    now: Optional[dt.datetime] = None,
) -> MtmPoint:
    """Скільки коштує конструкція, якщо ЗАРАЗ+days ціна = spot.

    Припущення (свідоме і задокументоване): поверхня волатильності
    «липне» до страйка (sticky-strike), тобто IV для того ж страйка і
    того ж ЗАЛИШКОВОГО терміну не змінюється. Для оцінки виходу з позиції
    це консервативно-нейтрально; чутливість до цього припущення бот
    показує окремим стрес-тестом vol +/- 10 в.п.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    t_left_claim = max(claim.years_to_deadline(now) - days_from_now / 365.0, 0.0)

    # --- нога предикт-маркету ---
    leg = structure.prediction
    if claim.kind is ClaimKind.TOUCH_ABOVE:
        yes_now = 1.0 if (touched or spot >= claim.threshold) else prob_touch(
            spot, claim.threshold, max(t_left_claim, 1e-6), surface.iv(claim.threshold, max(t_left_claim, 1e-6))
        )
    elif claim.kind is ClaimKind.TOUCH_BELOW:
        yes_now = 1.0 if (touched or spot <= claim.threshold) else prob_touch(
            spot, claim.threshold, max(t_left_claim, 1e-6), surface.iv(claim.threshold, max(t_left_claim, 1e-6))
        )
    else:
        if t_left_claim <= 0:
            yes_now = 1.0 if claim.resolves_yes(spot, spot, spot) else 0.0
        else:
            sig = surface.iv(claim.threshold, t_left_claim)
            f = spot * (surface.forward(t_left_claim) / surface.spot)
            yes_now = digital_call(f, claim.threshold, t_left_claim, sig, 1.0, surface.skew(claim.threshold, t_left_claim))
            if claim.kind is ClaimKind.BELOW_AT_EXPIRY:
                yes_now = 1.0 - yes_now

    unit = yes_now if leg.outcome.value == "yes" else 1.0 - yes_now
    value = leg.size * unit

    # --- опціонні ноги ---
    for ol in structure.options:
        t_left = max(ol.quote.years_to_expiry(now) - days_from_now / 365.0, 0.0)
        sign = 1.0 if ol.side.value == "buy" else -1.0
        if t_left <= 0:
            px = max(spot - ol.quote.strike, 0.0) if ol.quote.opt_type.value == "call" else max(
                ol.quote.strike - spot, 0.0
            )
        else:
            sig = surface.iv(ol.quote.strike, t_left)
            basis = surface.forward(t_left) / surface.spot
            px = black76(
                spot * basis, ol.quote.strike, t_left, sig, ol.quote.opt_type.value == "call"
            )
        value += sign * ol.qty * px

    return MtmPoint(
        days_from_now=days_from_now,
        spot=spot,
        value=value,
        pnl=value - structure.capital,
        touched=touched,
    )


# --------------------------------------------------------------------------- #
def make_pnl_fn(
    claim: EventClaim,
    surface: VolSurface,
    now: Optional[dt.datetime] = None,
    unwind_cost_frac: float = 0.04,
) -> Callable[[Structure, Scenario], float]:
    """P&L з урахуванням політики виходу.

    Сценарії з `unwind=True` означають: бар'єр торкнувся в момент tau,
    ставка згоріла, АЛЕ опціон у цей момент продається живим — на бар'єрі
    він як мінімум at-the-money і має повну часову вартість. Саме це
    перетворює «згорілу ставку» на керований збиток; при утриманні до
    експірації цієї вартості не існує.

    `unwind_cost_frac` — фрикція виходу (спред + комісія), у частках
    вартості опціонної ноги.
    """
    now = now or dt.datetime.now(dt.timezone.utc)

    def pnl(structure: Structure, sc: Scenario) -> float:
        if not sc.unwind or sc.tau_years is None:
            return structure.terminal_pnl(sc.yes_wins, sc.s_final)
        point = mark_to_market(
            structure,
            claim,
            surface,
            days_from_now=sc.tau_years * 365.0,
            spot=sc.s_final,
            touched=True,
            now=now,
        )
        gross_option_value = point.value  # нога предикшену в цей момент = 0
        return point.pnl - abs(gross_option_value) * unwind_cost_frac

    return pnl

"""Текстові звіти: картка можливості, сценарна таблиця, теплова карта MtM."""
from __future__ import annotations

import datetime as dt
import math
from typing import Optional

from .models import ClaimKind, Opportunity, Side, Structure
from .pricing.surface import VolSurface
from .strategy.payoff import mark_to_market

LINE = "─" * 78


def money(x: float) -> str:
    return f"{x:>10,.2f}"


def _limit_price(book, side: Side) -> Optional[float]:
    """Ціна топ-рівня книги — те, що дає лімітка «в ринок» без проковзування.

    Бот сам не вирішує, лімітка це чи маркет: рахує обидва сценарії, а
    яким заходити — вирішує людина під час купівлі.
    """
    return book.best_ask if side is Side.BUY else book.best_bid


def _leg_price_note(price: float, book, side: Side) -> str:
    """`ціна @ маркет | лімітка: X (топ книги)` — або без лімітки, якщо
    книга порожня (адаптер віддав лише останню угоду/mark, без глибини)."""
    limit = _limit_price(book, side)
    if limit is None or abs(limit - price) < 1e-9:
        return ""
    return f"  [лімітка по топу книги: {limit:.4f}]"


def opportunity_card(op: Opportunity) -> str:
    st, m = op.structure, op.metrics
    pm = st.prediction
    claim = pm.market.claim
    rows = [
        LINE,
        f"[{op.score:5.2f}] {st.name}   ({pm.market.venue} × "
        f"{st.options[0].quote.venue if st.options else '—'})",
        f"  ринок : {claim.raw_title or claim.kind.value}",
        f"  подія : {claim.asset} {claim.kind.value} {claim.threshold:,.0f}"
        + (f" (інший бар'єр гонки: {claim.race_other_threshold:,.0f})" if claim.is_race else "")
        + f" до {claim.deadline:%Y-%m-%d} ({m.days:.0f} дн.)",
        f"  ціна YES ринку : {m.market_prob:6.2%}   модель: {m.model_prob:6.2%}   "
        f"едж: {m.edge_pp:+.1f} в.п.",
        f"  IV (тенор {m.atm_iv_term_days:.0f}д, довідково): {m.atm_iv_near:6.1%}"
        f"  — нижче зазвичай читають як спокійніший режим для входу",
        "",
        f"  НОГА 1  {pm.outcome.value.upper():>3} × {pm.size:,.0f} @ {pm.price:.3f} (маркет)"
        f"  -> вартість {money(pm.cost)}"
        f"{_leg_price_note(pm.price, pm.market.book(pm.outcome), pm.side)}",
    ]
    for i, leg in enumerate(st.options, start=2):
        rows.append(
            f"  НОГА {i}  {leg.side.value.upper():>4} {leg.qty:g} × {leg.quote.symbol} "
            f"@ {leg.price:,.2f} (маркет)  -> {money(leg.cost)}"
            f"{_leg_price_note(leg.price, leg.quote.book, leg.side)}"
        )
    rows.append(
        "  (маркет = ціна проходом по стакану на весь розмір — саме на ній "
        "рахуються гарантії нижче; лімітка по топу — краща ціна, але без "
        "гарантії заповнення)"
    )
    rows += [
        "",
        f"  капітал        : {money(m.capital)}",
        f"  УМОВА A  прибуток ставки ≥ премія опціона      : "
        f"{'✓' if m.margin_a >= 0 else '✗'} запас {money(m.margin_a)}",
        f"  УМОВА B  профіт опціона на порозі ≥ ставка     : "
        f"{'✓' if m.margin_b >= 0 else '✗'} запас {money(m.margin_b)}",
        f"  найгірший P&L  : {money(m.worst_pnl)}  ({m.worst_return:+.2%})  [{m.worst_scenario}]",
        f"  CVaR 5%        : {money(m.cvar_pnl)}  ({m.cvar_return:+.2%})",
        f"  якщо ставка ВИГРАЛА, гірше за: {money(m.bet_win_floor)}",
        f"  якщо ставка ПРОГРАЛА, гірше за: {money(m.bet_lose_floor)}",
        f"  найкращий P&L  : {money(m.best_pnl)}",
        f"  очікуваний P&L : {money(m.ev_pnl)}  ({m.ev_return:+.2%}, ~{m.ev_apr:+.1%} річних)",
        f"  P(прибуток)    : {m.prob_profit:6.1%}   P(збиток): {m.prob_loss:6.1%}",
        f"  прапорці       : {', '.join(m.flags) if m.flags else '—'}",
    ]
    if st.note:
        rows.append(f"  логіка         : {st.note}")
    return "\n".join(rows)


def scenario_table(op: Opportunity, pnl_fn=None, buckets: int = 12) -> str:
    """Сценарна таблиця: P&L за рівнями ціни та за моментом торкання.

    `pnl_fn` має збігатися з політикою виходу, на якій рахувалися метрики,
    інакше таблиця суперечитиме картці можливості.
    """
    st = op.structure
    pnl_of = pnl_fn or (lambda structure, sc: structure.terminal_pnl(sc.yes_wins, sc.s_final))
    terminal = sorted([s for s in op.scenarios if not s.unwind], key=lambda s: s.s_final)
    touched = sorted([s for s in op.scenarios if s.unwind], key=lambda s: s.tau_years or 0.0)
    rows = [
        LINE,
        "  СЦЕНАРІЇ (спільний розподіл: результат ставки × ціна на експірації)",
        f"  {'ціна S_T':>12} {'ставка':>8} {'ймовірн.':>9} {'P&L':>12} {'ROI':>8}",
    ]
    if terminal:
        lo, hi = terminal[0].s_final, terminal[-1].s_final
        step = (math.log(hi) - math.log(lo)) / buckets if hi > lo else 1.0
        for i in range(buckets):
            b_lo = math.exp(math.log(lo) + step * i)
            b_hi = math.exp(math.log(lo) + step * (i + 1))
            for yes in (False, True):
                sel = [s for s in terminal if b_lo <= s.s_final < b_hi and s.yes_wins is yes]
                prob = sum(s.prob for s in sel)
                if prob < 5e-4:
                    continue
                mid = sum(s.s_final * s.prob for s in sel) / prob
                pnl = sum(pnl_of(st, s) * s.prob for s in sel) / prob
                rows.append(
                    f"  {mid:>12,.0f} {'YES' if yes else 'NO':>8} {prob:>8.2%} "
                    f"{pnl:>12,.0f} {pnl / st.capital:>7.1%}"
                )
    if touched:
        # окремо для сценаріїв, де ставка ПРОГРАЛА, і де ВИГРАЛА — інакше
        # для RACE_* (де "торкання" буває в обидва боки: наш бар'єр = програш,
        # інший бар'єр = виграш) вони б перемішались в один незрозумілий рядок
        rows += [
            "",
            "  РОЗХЕДЖ ДО ЕКСПІРАЦІЇ (подія відбулась достроково, опціон переоцінюється):",
            f"  {'через днів':>12} {'на рівні':>8} {'ставка':>7} {'ймовірн.':>9} {'P&L':>12} {'ROI':>8}",
        ]
        for yes in (True, False):
            side = [s for s in touched if s.yes_wins is yes]
            if not side:
                continue
            group = max(1, len(side) // 6)
            for i in range(0, len(side), group):
                chunk = side[i : i + group]
                prob = sum(s.prob for s in chunk)
                if prob < 5e-4:
                    continue
                tau = sum((s.tau_years or 0) * s.prob for s in chunk) / prob * 365
                pnl = sum(pnl_of(st, s) * s.prob for s in chunk) / prob
                lvl = sum(s.s_final * s.prob for s in chunk) / prob
                rows.append(
                    f"  {tau:>12,.0f} {lvl:>8,.0f} {'YES' if yes else 'NO':>7} {prob:>8.2%} "
                    f"{pnl:>12,.0f} {pnl / st.capital:>7.1%}"
                )
    return "\n".join(rows)


def mtm_heatmap(
    op: Opportunity,
    surface: VolSurface,
    now: Optional[dt.datetime] = None,
    price_steps: int = 9,
    time_steps: int = 6,
) -> str:
    """Чи справді «вийти можна майже будь-коли» — перевіряємо переоцінкою.

    Рядки — ціна ETH, стовпці — скільки днів минуло. У клітинці ROI позиції,
    якщо закрити її В ЦЕЙ момент за модельною ціною (без урахування спреду
    на виході — його додає окремий стрес-тест).
    """
    st = op.structure
    claim = st.prediction.market.claim
    if claim.is_race:
        return (
            f"{LINE}\n  ТЕПЛОВА КАРТА ВИХОДУ: не рахується для RACE_* — "
            "переоцінка ставки тут вимагає нового Монте-Карло на кожну "
            "клітинку (дорого); дивіться сценарну таблицю вище."
        )
    now = now or dt.datetime.now(dt.timezone.utc)
    total_days = claim.days_to_deadline(now)
    spot = surface.spot
    prices = [spot * (0.5 + 0.25 * i) for i in range(price_steps)]
    times = [total_days * i / time_steps for i in range(time_steps)]

    head = "  ціна\\днів " + "".join(f"{t:>9.0f}" for t in times)
    rows = [LINE, "  ТЕПЛОВА КАРТА ВИХОДУ (ROI при закритті позиції достроково)", head]
    for p in prices:
        cells = []
        for t in times:
            touched = claim.kind is ClaimKind.TOUCH_ABOVE and p >= claim.threshold
            pt = mark_to_market(st, claim, surface, t, p, touched=touched, now=now)
            cells.append(f"{pt.pnl / st.capital:>8.1%} ")
        rows.append(f"  {p:>9,.0f} " + "".join(cells))
    return "\n".join(rows)


def stress_test(op: Opportunity, surface: VolSurface, vol_shift_pp: float = 10.0) -> str:
    """Чутливість до волатильності: єдиний параметр, у якому ми найменш впевнені."""
    from copy import deepcopy

    claim = op.structure.prediction.market.claim
    if claim.is_race:
        return (
            f"{LINE}\n  СТРЕС-ТЕСТ IV: не рахується для RACE_* (те саме "
            "обмеження, що й у теплової карти — потрібне нове Монте-Карло)."
        )
    out = [LINE, f"  СТРЕС-ТЕСТ IV ±{vol_shift_pp:.0f} в.п. (миттєвий зсув поверхні)"]
    for shift in (-vol_shift_pp / 100.0, 0.0, vol_shift_pp / 100.0):
        s2 = deepcopy(surface)
        for sl in s2.slices:
            sl.ivs = [max(v + shift, 0.05) for v in sl.ivs]
        pt = mark_to_market(
            op.structure, op.structure.prediction.market.claim, s2, 0.0, s2.spot
        )
        out.append(
            f"    IV {shift*100:+5.0f} в.п.  ->  миттєва переоцінка {pt.pnl:>+10,.2f} "
            f"({pt.pnl / op.structure.capital:+.2%})"
        )
    out.append(
        "    (значення при зсуві 0 = модельний едж мінус вартість входу;\n"
        "     нахил ряду показує, чи позиція коротка за волатильністю)"
    )
    return "\n".join(out)

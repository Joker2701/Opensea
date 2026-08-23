"""Ймовірності подій, витягнуті з опціонного ринку.

Саме тут живе «спільна мова» між опціонами і предикт-маркетом:
обидва зводяться до ціни цифрового (digital) або бар'єрного контракту,
який платить $1 при настанні події.
"""
from __future__ import annotations

import math
from typing import Optional

from .bs import black76, d1_d2, norm_cdf, vega


# --------------------------------------------------------------------------- #
# Термінальні події: S_T > K  /  S_T < K
# --------------------------------------------------------------------------- #
def digital_call(
    f: float, k: float, t: float, sigma: float, df: float = 1.0, skew: float = 0.0
) -> float:
    """Ціна цифрового колла = -dC/dK.

    `skew` = dSigma/dK (нахил усмішки за страйком). Без нього ймовірність
    систематично зміщена: у крипті пут-скью робить «голий» N(d2) завищеним
    для низьких страйків і заниженим для високих.
    """
    _, d2 = d1_d2(f, k, t, sigma)
    return df * norm_cdf(d2) - vega(f, k, t, sigma, df) * skew


def digital_put(
    f: float, k: float, t: float, sigma: float, df: float = 1.0, skew: float = 0.0
) -> float:
    return df - digital_call(f, k, t, sigma, df, skew)


def digital_from_call_spread(
    c_lo: float, c_hi: float, k_lo: float, k_hi: float
) -> float:
    """Модельно-незалежна ймовірність з двох РЕАЛЬНИХ котирувань коллів.

    (C(k_lo) - C(k_hi)) / (k_hi - k_lo) — це рівно те, що можна купити
    на біржі як call-spread, тому це не «теоретична», а торгована ціна.
    """
    if k_hi <= k_lo:
        raise ValueError("k_hi має бути > k_lo")
    return max(min((c_lo - c_hi) / (k_hi - k_lo), 1.0), 0.0)


# --------------------------------------------------------------------------- #
# Бар'єрні події: max_{t<=T} S_t >= B  /  min <= B
# --------------------------------------------------------------------------- #
def prob_touch(
    s: float, b: float, t: float, sigma: float, mu: float = 0.0
) -> float:
    """Ймовірність торкнутися бар'єра B хоча б раз до T (формула відображення).

    mu — знос log-ціни у вимірі оцінювання (для крипти під ризик-нейтральною
    мірою mu = r - q ≈ ставка керрі форварда; за замовчуванням 0).
    Працює для B > S (touch up) і B < S (touch down).
    """
    if t <= 0 or sigma <= 0:
        return 1.0 if (b >= s if b > s else b <= s) and t <= 0 and abs(b - s) < 1e-12 else 0.0
    m = math.log(b / s)
    nu = mu - 0.5 * sigma * sigma
    vol = sigma * math.sqrt(t)
    if b > s:  # бар'єр зверху
        p = norm_cdf((-m + nu * t) / vol) + math.exp(2.0 * nu * m / (sigma * sigma)) * norm_cdf(
            (-m - nu * t) / vol
        )
    else:      # бар'єр знизу
        p = norm_cdf((m - nu * t) / vol) + math.exp(2.0 * nu * m / (sigma * sigma)) * norm_cdf(
            (m + nu * t) / vol
        )
    return min(max(p, 0.0), 1.0)


def prob_no_touch(s: float, b: float, t: float, sigma: float, mu: float = 0.0) -> float:
    return 1.0 - prob_touch(s, b, t, sigma, mu)


def prob_touch_and_finish_below(
    s: float, b: float, k: float, t: float, sigma: float, mu: float = 0.0
) -> float:
    """P(бар'єр B торкнувся) і (S_T <= K), K <= B, B > S.

    Це і є «зона болю» стратегії з постом інфлюенсера: ставка програла,
    а європейський колл до експірації встиг здутися. Рахуємо за принципом
    відображення: P(max>=B, S_T<=K) = (B/S)^(2nu/sigma^2) * P(S_T' <= B^2/(S*K)*S ... )
    у логарифмічних змінних це зводиться до одного N(.).
    """
    if b <= s or k > b:
        raise ValueError("очікується s < b та k <= b")
    if t <= 0 or sigma <= 0:
        return 0.0
    nu = mu - 0.5 * sigma * sigma
    vol = sigma * math.sqrt(t)
    m = math.log(b / s)
    x = math.log(k / s)
    # принцип відображення: рівень x «віддзеркалюється» у 2m - x
    z = (x - 2.0 * m - nu * t) / vol
    return math.exp(2.0 * nu * m / (sigma * sigma)) * norm_cdf(z)


def implied_prob_from_prediction_price(price: float) -> float:
    """Ціна YES у предикт-маркеті = ризик-нейтральна ймовірність (з поправкою
    на вартість грошей: капітал заморожений до резолву — див. docs/PLAN.md)."""
    return min(max(price, 0.0), 1.0)


def carry_adjusted_prob(prob: float, t: float, rate: float) -> Optional[float]:
    """Ймовірність, перерахована з урахуванням альтернативної вартості капіталу.

    Ставка в $0.63 з виплатою $1 через T років конкурує з безризиковою
    ставкою: справедлива ціна = prob * exp(-rate * T).
    """
    if t < 0:
        return None
    return prob * math.exp(-rate * t)


def first_passage_density(
    s: float, b: float, tau: float, sigma: float, mu: float = 0.0
) -> float:
    """Щільність ЧАСУ ПЕРШОГО ТОРКАННЯ бар'єра (обернено-гаусова).

    Навіщо: ставка на touch програє В МОМЕНТ торкання, а не на експірації.
    Якщо в цей момент опціон продати, його вартість = внутрішня + ЧАСОВА.
    Тому весь хедж треба оцінювати за розподілом tau, а не тільки за S_T.
    Інтеграл цієї щільності по [0, T] дорівнює prob_touch(...).
    """
    if tau <= 0 or sigma <= 0:
        return 0.0
    m = math.log(b / s)
    nu = mu - 0.5 * sigma * sigma
    return (
        abs(m)
        / (sigma * math.sqrt(2.0 * math.pi * tau**3))
        * math.exp(-((m - nu * tau) ** 2) / (2.0 * sigma * sigma * tau))
    )


def touch_time_buckets(
    s: float, b: float, t: float, sigma: float, mu: float = 0.0, n: int = 16
) -> list[tuple[float, float]]:
    """[(середина інтервалу tau, ймовірність торкнутись саме в ньому)].

    Сітка згущується на початку (перше торкання найімовірніше рано),
    а сумарна маса нормується на точне prob_touch.
    """
    if t <= 0:
        return []
    edges = [t * ((i / n) ** 1.5) for i in range(n + 1)]     # згущення до нуля
    out: list[tuple[float, float]] = []
    total = 0.0
    for i in range(n):
        lo, hi = edges[i], edges[i + 1]
        if hi <= lo:
            continue
        mid = 0.5 * (lo + hi)
        # проста квадратура Сімпсона на інтервалі
        f_lo = first_passage_density(s, b, max(lo, 1e-9), sigma, mu)
        f_mid = first_passage_density(s, b, mid, sigma, mu)
        f_hi = first_passage_density(s, b, hi, sigma, mu)
        mass = (hi - lo) / 6.0 * (f_lo + 4 * f_mid + f_hi)
        out.append((mid, mass))
        total += mass
    target = prob_touch(s, b, t, sigma, mu)
    if total > 0:
        out = [(mid, mass / total * target) for mid, mass in out]
    return out

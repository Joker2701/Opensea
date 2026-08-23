"""Модельна ймовірність події предикт-маркету, витягнута з опціонів.

Це серце «спільної мови»: EventClaim + VolSurface -> ймовірність,
яку можна напряму порівняти з ціною YES на Polymarket/Kalshi.
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from typing import Optional

from ..models import ClaimKind, EventClaim
from .digital import digital_call, digital_put, prob_touch
from .race import simulate_race
from .surface import VolSurface


@dataclass
class ModelProb:
    prob: float
    method: str
    sigma: float
    t: float
    forward: float
    extrapolated: bool = False     # термін події виходить за межі лістингу опціонів
    note: str = ""


def model_probability(
    claim: EventClaim,
    surface: VolSurface,
    now: Optional[dt.datetime] = None,
    rate: float = 0.0,
) -> ModelProb:
    """Ризик-нейтральна ймовірність YES за опціонною поверхнею.

    Для TOUCH_* використовується формула відображення з волатильністю,
    взятою НА РІВНІ БАР'ЄРА (не ATM) — це принципово: скью на страйку 4000
    може відрізнятись від ATM на 5-15 вол-пунктів, а ймовірність торкання
    до неї дуже чутлива.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    t = max(claim.years_to_deadline(now), 1e-6)
    f = surface.forward(t)
    sigma = surface.iv(claim.threshold, t)
    extrapolated = t > surface.max_listed_t() * 1.001
    mu = math.log(f / surface.spot) / t if t > 0 else 0.0
    skew = surface.skew(claim.threshold, t)

    if claim.kind is ClaimKind.TOUCH_ABOVE:
        p = prob_touch(surface.spot, claim.threshold, t, sigma, mu)
        method = "reflection/one-touch"
    elif claim.kind is ClaimKind.TOUCH_BELOW:
        p = prob_touch(surface.spot, claim.threshold, t, sigma, mu)
        method = "reflection/one-touch"
    elif claim.kind is ClaimKind.ABOVE_AT_EXPIRY:
        p = digital_call(f, claim.threshold, t, sigma, 1.0, skew)
        method = "digital(call)+skew"
    elif claim.kind is ClaimKind.BELOW_AT_EXPIRY:
        p = digital_put(f, claim.threshold, t, sigma, 1.0, skew)
        method = "digital(put)+skew"
    elif claim.kind is ClaimKind.RANGE_AT_EXPIRY:
        hi = claim.threshold_hi
        if hi is None:
            raise ValueError("RANGE_AT_EXPIRY без верхньої межі")
        s_hi = surface.iv(hi, t)
        p = digital_call(f, claim.threshold, t, sigma, 1.0, skew) - digital_call(
            f, hi, t, s_hi, 1.0, surface.skew(hi, t)
        )
        method = "digital spread"
    elif claim.is_race:
        other = claim.race_other_threshold
        if other is None:
            raise ValueError("RACE_* без другого бар'єра (race_other_threshold)")
        upper, lower = (claim.threshold, other) if claim.kind is ClaimKind.RACE_UPPER_FIRST \
            else (other, claim.threshold)
        # без замкненої формули для скінченного часу (див. pricing/race.py) —
        # рахуємо Монте-Карло. Фіксований seed -> та сама можливість завжди
        # дає той самий едж між скануваннями, а не "мерехтить" від запуску.
        res = simulate_race(
            surface.spot, lower, upper, t, sigma, mu,
            n_paths=8_000, n_steps=200, keep_outcomes=False,
        )
        p = res.p_upper if claim.kind is ClaimKind.RACE_UPPER_FIRST else res.p_lower
        method = "monte-carlo/race"
    else:
        raise ValueError(claim.kind)

    return ModelProb(
        prob=min(max(p, 0.0), 1.0),
        method=method,
        sigma=sigma,
        t=t,
        forward=f,
        extrapolated=extrapolated,
        note="термін події довший за найдальшу лістовану експірацію — IV екстрапольована"
        if extrapolated
        else "",
    )


def fair_prediction_price(model: ModelProb, rate: float = 0.0) -> float:
    """Справедлива ціна YES з урахуванням заморозки капіталу до резолву.

    На предикт-маркеті ви платите СЬОГОДНІ, а отримуєте $1 у день резолву,
    тому чесний бенчмарк — дисконтована ймовірність.
    """
    return model.prob * math.exp(-rate * model.t)

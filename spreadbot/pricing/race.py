"""Гонка до одного з двох бар'єрів: «яка ціна буде раніше — X чи Y».

Для ОДНОГО бар'єра є точна формула (відображення, `pricing/digital.py`).
Для ДВОХ одночасно точної елементарної формули немає — є класичний ряд
через нескінченні відображення (метод зображень), але його легко
переплутати знаком чи індексом, а звірити з довідником зараз неможливо
(мережа заблокована). Тому тут — Монте-Карло: гарантовано правильний за
побудовою метод (ми буквально симулюємо визначення події, а не формулу),
з точною поправкою на «торкання бар'єра ВСЕРЕДИНІ кроку симуляції»
(стандартний трюк для Monte Carlo бар'єрних опціонів, Glasserman ch. 6) —
без неї грубий крок симуляції систематично занижує ймовірність торкання.

Компроміс: результат детермінований лише за фіксованим seed, і повільніший
за замкнену формулу. Але порахований один раз на ринок при скануванні
(не в гарячому циклі солвера), тому продуктивність не критична.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class RaceOutcome:
    """Результат для однієї симуляції: що і коли сталось першим."""

    winner: str          # "upper" | "lower" | "neither"
    tau_years: Optional[float]   # момент торкання (None, якщо "neither")
    s_final: float        # ціна на момент завершення (торкання або T)


@dataclass
class RaceResult:
    p_upper: float
    p_lower: float
    p_neither: float
    outcomes: list[RaceOutcome] = field(default_factory=list)

    def __post_init__(self) -> None:
        total = self.p_upper + self.p_lower + self.p_neither
        if total > 0:
            self.p_upper /= total
            self.p_lower /= total
            self.p_neither /= total


def _bridge_cross_prob(x0: float, x1: float, level: float, sigma: float, dt: float) -> float:
    """P(міст Q(0)=x0 -> Q(dt)=x1 торкнувся `level` десь усередині кроку).

    Формула Брауніва мосту (Glasserman, "Monte Carlo Methods in Financial
    Engineering", §6.4): якщо `level` по один бік від ОБОХ кінців кроку,
    ймовірність торкання = exp(-2·(level−x0)·(level−x1) / (σ²·dt)).
    Якщо кінці вже по різні боки — крок сам перетнув рівень, торкання 100%.
    """
    d0, d1 = level - x0, level - x1
    if d0 == 0.0 or d1 == 0.0:
        return 1.0
    if (d0 > 0) != (d1 > 0):
        return 1.0  # дискретний крок уже перестрибнув рівень
    return math.exp(-2.0 * d0 * d1 / (sigma * sigma * dt))


def simulate_race(
    spot: float,
    lower: float,
    upper: float,
    t: float,
    sigma: float,
    mu: float = 0.0,
    n_paths: int = 12_000,
    n_steps: int = 250,
    seed: int = 20260823,
    keep_outcomes: bool = True,
) -> RaceResult:
    """Монте-Карло: який з двох бар'єрів торкнеться раніше (і коли).

    `mu` — знос ЛОГ-ціни під мірою оцінювання (як і в решті pricing/*, для
    крипти ≈ carry форварда). Working у лог-просторі: X_t = ln(S_t/S0)
    рухається як X ~ N(mu - 0.5σ², σ²) за одиницю часу.
    """
    if not (lower < spot < upper):
        raise ValueError("очікується lower < spot < upper")
    a = math.log(lower / spot)   # < 0
    b = math.log(upper / spot)   # > 0
    dt = t / n_steps
    nu = mu - 0.5 * sigma * sigma
    drift = nu * dt
    vol = sigma * math.sqrt(dt)

    rng = random.Random(seed)
    p_upper = p_lower = p_neither = 0.0
    outcomes: list[RaceOutcome] = []

    for i in range(n_paths):
        # антитетичні пари зменшують дисперсію вдвічі за той самий n_paths
        antithetic = i % 2 == 1
        x = 0.0
        winner: Optional[str] = None
        tau: Optional[float] = None
        for step in range(n_steps):
            z = rng.gauss(0.0, 1.0)
            if antithetic:
                z = -z
            x_new = x + drift + vol * z

            if x_new >= b or x_new <= a:
                winner = "upper" if x_new >= b else "lower"
                tau = (step + 1) * dt
                x = x_new
                break

            # точна ймовірність, що торкнулись УСЕРЕДИНІ кроку, хоч на
            # кінцях і не вийшли за межі
            p_hit_b = _bridge_cross_prob(x, x_new, b, sigma, dt)
            p_hit_a = _bridge_cross_prob(x, x_new, a, sigma, dt)
            u = rng.random()
            if u < p_hit_b:
                winner, tau, x = "upper", (step + rng.random()) * dt, x_new
                break
            if u < p_hit_b + p_hit_a:
                winner, tau, x = "lower", (step + rng.random()) * dt, x_new
                break
            x = x_new

        if winner is None:
            winner = "neither"

        if winner == "upper":
            p_upper += 1.0
        elif winner == "lower":
            p_lower += 1.0
        else:
            p_neither += 1.0

        if keep_outcomes:
            s_final = spot * math.exp(b if winner == "upper" else a if winner == "lower" else x)
            outcomes.append(RaceOutcome(winner=winner, tau_years=tau, s_final=s_final))

    n = float(n_paths)
    return RaceResult(p_upper=p_upper / n, p_lower=p_lower / n, p_neither=p_neither / n,
                       outcomes=outcomes)


def ruin_probability_infinite_horizon(spot: float, lower: float, upper: float,
                                       sigma: float, mu: float = 0.0) -> float:
    """P(торкнеться upper раніше за lower) на НЕСКІНЧЕННОМУ горизонті.

    Замкнена форма (класична "gambler's ruin" для арифметичного БР із
    зносом) — існує, бо на t->inf ймовірність "жодного" зникає. Це не
    те, що треба для реальних ринків (у них є дедлайн), але це ТОЧКА
    ЗВІРКИ: симуляція з дуже великим T мусить збігатись до цієї формули.
    Похідна: f(x) = (1 - e^{-2ν(x-a)/σ²}) / (1 - e^{-2ν(b-a)/σ²}) —
    розв'язок ν·f' + 0.5σ²·f'' = 0 з f(a)=0, f(b)=1 (перевірено підстановкою).
    """
    a = math.log(lower / spot)
    b = math.log(upper / spot)
    nu = mu - 0.5 * sigma * sigma
    if abs(nu) < 1e-12:
        return -a / (b - a)   # без зносу -> лінійна інтерполяція (частка відстані)
    num = 1.0 - math.exp(-2.0 * nu * (0.0 - a) / (sigma * sigma))
    den = 1.0 - math.exp(-2.0 * nu * (b - a) / (sigma * sigma))
    return num / den

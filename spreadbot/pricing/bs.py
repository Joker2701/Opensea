"""Блек-76 (опціон на форвард) + греки + імпліцитна волатильність.

Свідомо без numpy/scipy — щоб каркас запускався будь-де.
Крипто-опціони на Deribit/Bybit/OKX котируються від форварда, тому
базова модель саме Black-76, а не Блек-Шоулз від споту.
"""
from __future__ import annotations

import math
from typing import Optional

SQRT_2PI = math.sqrt(2.0 * math.pi)


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / SQRT_2PI


def d1_d2(f: float, k: float, t: float, sigma: float) -> tuple[float, float]:
    if f <= 0 or k <= 0 or t <= 0 or sigma <= 0:
        raise ValueError("f, k, t, sigma мають бути > 0")
    vol = sigma * math.sqrt(t)
    d1 = (math.log(f / k) + 0.5 * sigma * sigma * t) / vol
    return d1, d1 - vol


def black76(
    f: float, k: float, t: float, sigma: float, is_call: bool, df: float = 1.0
) -> float:
    """Ціна опціону в валюті котирування (USD за 1 одиницю базового активу)."""
    if t <= 0 or sigma <= 0:
        intrinsic = max(f - k, 0.0) if is_call else max(k - f, 0.0)
        return df * intrinsic
    d1, d2 = d1_d2(f, k, t, sigma)
    if is_call:
        return df * (f * norm_cdf(d1) - k * norm_cdf(d2))
    return df * (k * norm_cdf(-d2) - f * norm_cdf(-d1))


def delta(f: float, k: float, t: float, sigma: float, is_call: bool, df: float = 1.0) -> float:
    d1, _ = d1_d2(f, k, t, sigma)
    return df * (norm_cdf(d1) if is_call else norm_cdf(d1) - 1.0)


def vega(f: float, k: float, t: float, sigma: float, df: float = 1.0) -> float:
    """dPrice/dSigma (на 1.0 вол, тобто на 100 вол-пунктів)."""
    d1, _ = d1_d2(f, k, t, sigma)
    return df * f * norm_pdf(d1) * math.sqrt(t)


def gamma(f: float, k: float, t: float, sigma: float, df: float = 1.0) -> float:
    d1, _ = d1_d2(f, k, t, sigma)
    return df * norm_pdf(d1) / (f * sigma * math.sqrt(t))


def theta(
    f: float, k: float, t: float, sigma: float, is_call: bool, df: float = 1.0
) -> float:
    """Тета на рік (поділіть на 365 для денної)."""
    d1, _ = d1_d2(f, k, t, sigma)
    return -df * f * norm_pdf(d1) * sigma / (2.0 * math.sqrt(t))


def implied_vol(
    price: float,
    f: float,
    k: float,
    t: float,
    is_call: bool,
    df: float = 1.0,
    lo: float = 1e-4,
    hi: float = 5.0,
    tol: float = 1e-8,
    max_iter: int = 200,
) -> Optional[float]:
    """Бісекція по волатильності. None, якщо ціна поза no-arbitrage межами."""
    if t <= 0 or price <= 0:
        return None
    intrinsic = df * (max(f - k, 0.0) if is_call else max(k - f, 0.0))
    upper = df * (f if is_call else k)
    if price <= intrinsic - 1e-12 or price >= upper:
        return None
    for _ in range(max_iter):
        mid = 0.5 * (lo + hi)
        diff = black76(f, k, t, mid, is_call, df) - price
        if abs(diff) < tol:
            return mid
        if diff > 0:
            hi = mid
        else:
            lo = mid
    return 0.5 * (lo + hi)

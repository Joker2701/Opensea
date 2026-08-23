"""Поверхня волатильності: інтерполяція IV за страйком і терміном.

Мінімалістична, але коректна за конструкцією:
  * по страйку — лінійно в log-moneyness k = ln(K/F), плоска екстраполяція;
  * по терміну — лінійно в ЗАГАЛЬНІЙ дисперсії w = sigma^2 * T (це те, що не
    створює календарного арбітражу, на відміну від інтерполяції самої sigma);
  * skew(K, T) — чисельна похідна dSigma/dK для цифрових опціонів.
"""
from __future__ import annotations

import bisect
import datetime as dt
import math
from dataclasses import dataclass, field
from typing import Optional

from ..models import OptionChain, OptionType
from .bs import implied_vol


@dataclass
class Slice:
    """Зріз поверхні на одну експірацію."""

    t: float                      # роки до експірації
    forward: float
    ks: list[float] = field(default_factory=list)      # log-moneyness, зростає
    ivs: list[float] = field(default_factory=list)

    def iv_at_k(self, k: float) -> float:
        if not self.ks:
            raise ValueError("порожній зріз")
        if k <= self.ks[0]:
            return self.ivs[0]
        if k >= self.ks[-1]:
            return self.ivs[-1]
        i = bisect.bisect_right(self.ks, k) - 1
        k0, k1 = self.ks[i], self.ks[i + 1]
        v0, v1 = self.ivs[i], self.ivs[i + 1]
        w = (k - k0) / (k1 - k0)
        return v0 + w * (v1 - v0)


class VolSurface:
    def __init__(self, slices: list[Slice], spot: float, rate: float = 0.0):
        self.slices = sorted(slices, key=lambda s: s.t)
        self.spot = spot
        self.rate = rate
        if not self.slices:
            raise ValueError("поверхня без зрізів")

    # ------------------------------------------------------------------ #
    @classmethod
    def from_chain(
        cls, chain: OptionChain, now: Optional[dt.datetime] = None, use_mark: bool = True
    ) -> "VolSurface":
        now = now or chain.fetched_at
        slices: list[Slice] = []
        for expiry in chain.expiries():
            t = max((expiry - now).total_seconds() / (365 * 86400.0), 1e-6)
            f = chain.forward(expiry)
            pts: dict[float, float] = {}
            for q in chain.by_expiry(expiry):
                iv = q.iv_mark
                if iv is None:
                    px = q.mark if use_mark else None
                    if px is None and q.bid is not None and q.ask is not None:
                        px = 0.5 * (q.bid + q.ask)
                    if px is None:
                        continue
                    iv = implied_vol(px, f, q.strike, t, q.opt_type is OptionType.CALL)
                if iv is None or iv <= 0:
                    continue
                # OTM-котирування надійніші: пут нижче форварда, колл вище
                otm = (q.opt_type is OptionType.CALL) == (q.strike >= f)
                k = math.log(q.strike / f)
                if k not in pts or otm:
                    pts[k] = iv
            if len(pts) >= 2:
                ks = sorted(pts)
                slices.append(Slice(t=t, forward=f, ks=ks, ivs=[pts[k] for k in ks]))
        return cls(slices, spot=chain.spot, rate=chain.risk_free)

    # ------------------------------------------------------------------ #
    def forward(self, t: float) -> float:
        """Форвард на довільний термін (лог-лінійно за базисом)."""
        ts = [s.t for s in self.slices]
        if t <= ts[0]:
            s = self.slices[0]
            r = math.log(s.forward / self.spot) / s.t
            return self.spot * math.exp(r * t)
        if t >= ts[-1]:
            s = self.slices[-1]
            r = math.log(s.forward / self.spot) / s.t
            return self.spot * math.exp(r * t)
        i = bisect.bisect_right(ts, t) - 1
        a, b = self.slices[i], self.slices[i + 1]
        w = (t - a.t) / (b.t - a.t)
        return math.exp(math.log(a.forward) + w * (math.log(b.forward) - math.log(a.forward)))

    def iv(self, strike: float, t: float) -> float:
        """IV для страйка і терміну (інтерполяція у загальній дисперсії)."""
        t = max(t, 1e-6)
        ts = [s.t for s in self.slices]
        f = self.forward(t)
        k = math.log(strike / f)
        if t <= ts[0]:
            return self.slices[0].iv_at_k(k)
        if t >= ts[-1]:
            return self.slices[-1].iv_at_k(k)
        i = bisect.bisect_right(ts, t) - 1
        a, b = self.slices[i], self.slices[i + 1]
        wa = a.iv_at_k(k) ** 2 * a.t
        wb = b.iv_at_k(k) ** 2 * b.t
        w = wa + (t - a.t) / (b.t - a.t) * (wb - wa)
        return math.sqrt(max(w, 1e-12) / t)

    def skew(self, strike: float, t: float, h_rel: float = 0.01) -> float:
        """dSigma/dK — потрібна для коректної ціни цифрового опціону."""
        h = max(strike * h_rel, 1e-6)
        return (self.iv(strike + h, t) - self.iv(strike - h, t)) / (2.0 * h)

    def atm(self, t: float) -> float:
        return self.iv(self.forward(t), t)

    def max_listed_t(self) -> float:
        return self.slices[-1].t

    def summary(self) -> str:
        rows = []
        for s in self.slices:
            rows.append(
                f"  T={s.t*365:7.1f}d  F={s.forward:9.2f}  ATM≈{s.iv_at_k(0.0)*100:5.1f}%  "
                f"страйків={len(s.ks)}  k∈[{s.ks[0]:+.2f},{s.ks[-1]:+.2f}]"
            )
        return "\n".join(rows)

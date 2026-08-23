"""Оркестратор: ринки -> модельна ймовірність -> едж -> конструкції -> ранжування."""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Optional

from ..adapters.base import OptionsAdapter, PredictionAdapter
from ..config import Config
from ..models import Opportunity, Outcome, PredictionMarket
from ..pricing.events import ModelProb, model_probability
from ..pricing.surface import VolSurface
from ..strategy.constructors import build_candidates
from ..strategy.costs import DEFAULT_OPTION_FEES, DEFAULT_PREDICTION_FEES, OptionFees, PredictionFees
from ..strategy.payoff import build_scenarios, make_pnl_fn
from ..strategy.sizing import solve
from .ranking import rank

log = logging.getLogger(__name__)


@dataclass
class MarketView:
    """Проміжний зріз: ринок + модель + сирий едж (ще без конструкції)."""

    market: PredictionMarket
    model: ModelProb
    market_yes: float
    edge_pp: float
    direction: Outcome
    flags: list[str] = field(default_factory=list)


class Scanner:
    def __init__(
        self,
        prediction_adapters: list[PredictionAdapter],
        options_adapters: list[OptionsAdapter],
        cfg: Optional[Config] = None,
    ):
        self.pred = prediction_adapters
        self.opts = options_adapters
        self.cfg = cfg or Config()

    # ------------------------------------------------------------------ #
    def load_surfaces(self) -> dict[tuple[str, str], tuple]:
        """(asset, venue) -> (chain, surface). Одна закачка на цикл сканування."""
        out = {}
        for adapter in self.opts:
            for asset in self.cfg.scan.assets:
                try:
                    chain = adapter.fetch_chain(asset)
                    if not chain.quotes:
                        continue
                    surface = VolSurface.from_chain(chain)
                    out[(asset, adapter.venue)] = (chain, surface)
                    log.info("%s %s: %d котирувань, %d експірацій",
                             adapter.venue, asset, len(chain.quotes), len(chain.expiries()))
                except Exception as exc:  # noqa: BLE001
                    log.warning("не вдалося завантажити ланцюг %s/%s: %s", adapter.venue, asset, exc)
        return out

    # ------------------------------------------------------------------ #
    def screen(self, surfaces: dict) -> list[MarketView]:
        """Швидкий фільтр за еджем — ДО важких запитів стаканів."""
        views: list[MarketView] = []
        now = dt.datetime.now(dt.timezone.utc)
        for adapter in self.pred:
            try:
                markets = adapter.list_markets(
                    assets=self.cfg.scan.assets,
                    min_days=self.cfg.scan.min_days,
                    max_days=self.cfg.scan.max_days,
                    limit=self.cfg.scan.max_markets,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("%s: список ринків не завантажився: %s", adapter.venue, exc)
                continue

            for m in markets:
                if m.volume_usd < self.cfg.scan.min_volume_usd:
                    continue
                key = next(
                    ((m.claim.asset, v) for (a, v) in surfaces if a == m.claim.asset), None
                )
                if key is None:
                    continue
                chain, surface = surfaces[key]
                try:
                    model = model_probability(m.claim, surface, now, self.cfg.rate_usd)
                except Exception as exc:  # noqa: BLE001
                    log.debug("модель не порахувалась для %s: %s", m.market_id, exc)
                    continue
                mkt_yes = m.implied_yes
                if mkt_yes is None:
                    continue
                edge = (mkt_yes - model.prob) * 100.0
                flags = []
                if model.extrapolated:
                    flags.append("extrapolated_iv")
                if self.cfg.risk.require_known_resolution_source and (
                    m.claim.resolution_source in ("", "unknown")
                ):
                    flags.append("unknown_resolution")
                for bad in self.cfg.risk.blocked_resolution_keywords:
                    if bad.lower() in (m.claim.resolution_source or "").lower():
                        flags.append("blocked_resolution")
                if abs(edge) < self.cfg.scan.min_edge_pp:
                    continue
                views.append(
                    MarketView(
                        market=m,
                        model=model,
                        market_yes=mkt_yes,
                        edge_pp=edge,
                        direction=Outcome.NO if edge > 0 else Outcome.YES,
                        flags=flags,
                    )
                )
        views.sort(key=lambda v: abs(v.edge_pp), reverse=True)
        return views

    # ------------------------------------------------------------------ #
    def build(self, views: list[MarketView], surfaces: dict) -> list[Opportunity]:
        ops: list[Opportunity] = []
        now = dt.datetime.now(dt.timezone.utc)
        for view in views:
            if "blocked_resolution" in view.flags:
                continue
            m = view.market
            key = next(((m.claim.asset, v) for (a, v) in surfaces if a == m.claim.asset), None)
            if key is None:
                continue
            chain, surface = surfaces[key]

            adapter = next((a for a in self.pred if a.venue == m.venue), None)
            if adapter is not None:
                try:
                    adapter.load_books(m)
                except Exception as exc:  # noqa: BLE001
                    log.debug("стакани %s не завантажились: %s", m.market_id, exc)

            policy = self.cfg.sizing.exit_policy
            scenarios = build_scenarios(
                m.claim, surface, now, n=self.cfg.scan.scenario_points, policy=policy
            )
            pnl_fn = (
                make_pnl_fn(m.claim, surface, now, self.cfg.sizing.unwind_cost_frac)
                if policy == "unwind"
                else None
            )
            cands = build_candidates(
                m,
                chain,
                strike_ratios=self.cfg.scan.strike_ratios,
                include_spreads=self.cfg.scan.include_spreads,
                max_expiry_gap_days=self.cfg.risk.allow_expiry_gap_days,
            )
            pm_fees = DEFAULT_PREDICTION_FEES.get(m.venue, PredictionFees())
            opt_fees = DEFAULT_OPTION_FEES.get(chain.venue, OptionFees())
            days = m.claim.days_to_deadline(now)

            for cand in cands:
                if cand.outcome is not view.direction:
                    continue
                res = solve(
                    cand,
                    scenarios,
                    chain.spot,
                    days,
                    self.cfg.sizing,
                    pm_fees,
                    opt_fees,
                    market_prob=view.market_yes,
                    model_prob=view.model.prob,
                    pnl_fn=pnl_fn,
                )
                if res is None:
                    continue
                res.metrics.flags = list(view.flags) + list(cand.tags)
                if res.metrics.edge_pp < self.cfg.scan.min_net_edge_pp:
                    continue
                ops.append(
                    Opportunity(structure=res.structure, metrics=res.metrics, scenarios=scenarios)
                )
        return rank(ops)

    # ------------------------------------------------------------------ #
    def run(self) -> list[Opportunity]:
        return self.run_with_surfaces()[0]

    def run_with_surfaces(self) -> tuple[list[Opportunity], dict]:
        """Те саме, що run(), але додатково повертає завантажені поверхні
        волатильності — щоб зовнішній код (напр. моніторинг позицій у
        Telegram-боті) міг узяти поточний спот, не сканувавши ринок вдруге."""
        surfaces = self.load_surfaces()
        if not surfaces:
            log.error("жодної опціонної поверхні — сканування неможливе")
            return [], surfaces
        views = self.screen(surfaces)
        log.info("ринків з еджем >= %.1f в.п.: %d", self.cfg.scan.min_edge_pp, len(views))
        ops = self.build(views, surfaces)
        return ops[: self.cfg.scan.top_n], surfaces

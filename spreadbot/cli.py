"""CLI бота.

  python -m spreadbot demo                 # повний прогін на фікстурах (без мережі)
  python -m spreadbot scan -c config.yaml  # реальне сканування
  python -m spreadbot chain --asset ETH    # поверхня волатильності
  python -m spreadbot markets              # які ринки бот розпізнав
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import sys

from .adapters.offline import FIXTURE_DIR, offline_deribit, offline_polymarket
from .adapters.options.bybit import BybitAdapter
from .adapters.options.deribit import DeribitAdapter
from .adapters.prediction.kalshi import KalshiAdapter
from .adapters.prediction.polymarket import PolymarketAdapter
from .config import Config
from .pricing.events import model_probability
from .pricing.surface import VolSurface
from .report import LINE, mtm_heatmap, opportunity_card, scenario_table, stress_test
from .scanner.scanner import Scanner
from .strategy.payoff import make_pnl_fn
from .storage.db import Storage

PRED = {"polymarket": PolymarketAdapter, "kalshi": KalshiAdapter}
OPTS = {"deribit": DeribitAdapter, "bybit": BybitAdapter}


def _adapters(cfg: Config):
    if cfg.offline:
        pm = cfg.fixtures.get("polymarket", os.path.join(FIXTURE_DIR, "polymarket_crypto.json"))
        dr = cfg.fixtures.get("deribit", os.path.join(FIXTURE_DIR, "deribit_eth.json"))
        return [offline_polymarket(pm)], [offline_deribit(dr)]
    pred = [PRED[v](cache_dir=cfg.cache_dir) for v in cfg.scan.prediction_venues if v in PRED]
    opts = [OPTS[v](cache_dir=cfg.cache_dir) for v in cfg.scan.option_venues if v in OPTS]
    return pred, opts


def cmd_scan(args, cfg: Config) -> int:
    pred, opts = _adapters(cfg)
    scanner = Scanner(pred, opts, cfg)
    ops = scanner.run()
    if not ops:
        print("Можливостей за поточними фільтрами не знайдено.")
        return 0
    store = Storage(cfg.db_path) if args.save else None
    for op in ops:
        print(opportunity_card(op))
        if args.verbose:
            print(scenario_table(op))
        if store:
            store.save_opportunity(op)
    if store:
        store.close()
    print(LINE)
    print(f"Знайдено {len(ops)} конструкцій.")
    return 0


def cmd_demo(args, cfg: Config) -> int:
    """Повний прогін офлайн + глибокий розбір найкращої конструкції."""
    cfg.offline = True
    cfg.scan.assets = ["ETH"]
    cfg.scan.prediction_venues = ["polymarket"]
    cfg.scan.option_venues = ["deribit"]
    pred, opts = _adapters(cfg)
    scanner = Scanner(pred, opts, cfg)
    surfaces = scanner.load_surfaces()
    (chain, surface) = next(iter(surfaces.values()))

    print(LINE)
    print(f"  ПОВЕРХНЯ ВОЛАТИЛЬНОСТІ {chain.venue}/{chain.underlying}  спот={chain.spot:,.2f}")
    print(surface.summary())

    views = scanner.screen(surfaces)
    print(LINE)
    print("  РИНКИ ТА ЕДЖ")
    for v in views:
        print(
            f"  {v.market.claim.raw_title[:52]:52} ринок={v.market_yes:6.2%} "
            f"модель={v.model.prob:6.2%} ({v.model.method}) едж={v.edge_pp:+5.1f} в.п. "
            f"-> купувати {v.direction.value.upper()}"
        )

    ops = scanner.build(views, surfaces)
    if not ops:
        print("Конструкцій, що проходять ризик-ліміти, немає.")
        return 1
    for op in ops[: cfg.scan.top_n]:
        print(opportunity_card(op))
    best = ops[0]
    claim = best.structure.prediction.market.claim
    pnl_fn = (
        make_pnl_fn(claim, surface, unwind_cost_frac=cfg.sizing.unwind_cost_frac)
        if cfg.sizing.exit_policy == "unwind"
        else None
    )
    print(scenario_table(best, pnl_fn))
    print(mtm_heatmap(best, surface))
    print(stress_test(best, surface, cfg.risk.vol_stress_pp))
    return 0


def cmd_chain(args, cfg: Config) -> int:
    _, opts = _adapters(cfg)
    for adapter in opts:
        chain = adapter.fetch_chain(args.asset)
        surface = VolSurface.from_chain(chain)
        print(f"{adapter.venue}/{args.asset}: спот={chain.spot:,.2f}, "
              f"{len(chain.quotes)} котирувань")
        print(surface.summary())
    return 0


def cmd_markets(args, cfg: Config) -> int:
    pred, opts = _adapters(cfg)
    surfaces = Scanner(pred, opts, cfg).load_surfaces()
    now = dt.datetime.now(dt.timezone.utc)
    for adapter in pred:
        for m in adapter.list_markets(
            assets=cfg.scan.assets, min_days=cfg.scan.min_days, max_days=cfg.scan.max_days
        ):
            key = next(((m.claim.asset, v) for (a, v) in surfaces if a == m.claim.asset), None)
            model = ""
            if key:
                mp = model_probability(m.claim, surfaces[key][1], now, cfg.rate_usd)
                model = f"модель={mp.prob:6.2%} [{mp.method}]"
            print(
                f"{m.venue:11} {m.claim.asset:4} {m.claim.kind.value:16} "
                f"{m.claim.threshold:>9,.0f} {m.claim.deadline:%Y-%m-%d} "
                f"ринок={(m.implied_yes or 0):6.2%} {model}  {m.claim.raw_title[:45]}"
            )
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser("spreadbot", description="Пошук вилок опціон × предикт-маркет")
    p.add_argument("-c", "--config", default="config/config.yaml")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--offline", action="store_true", help="працювати на фікстурах")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="просканувати ринки")
    s.add_argument("--save", action="store_true", help="писати знахідки в SQLite")
    s.set_defaults(func=cmd_scan)

    d = sub.add_parser("demo", help="офлайн-демо на фікстурах")
    d.set_defaults(func=cmd_demo)

    c = sub.add_parser("chain", help="показати поверхню волатильності")
    c.add_argument("--asset", default="ETH")
    c.set_defaults(func=cmd_chain)

    m = sub.add_parser("markets", help="показати розпізнані ринки")
    m.set_defaults(func=cmd_markets)

    args = p.parse_args(argv)
    cfg = Config.load(args.config)
    if args.offline:
        cfg.offline = True
    logging.basicConfig(
        level=getattr(logging, "DEBUG" if args.verbose else cfg.log_level),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    return args.func(args, cfg)


if __name__ == "__main__":
    sys.exit(main())

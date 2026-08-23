"""Bybit v5 — USDC-опціони (лінійні, премія одразу в USDC).

  GET /v5/market/instruments-info?category=option&baseCoin=ETH
  GET /v5/market/tickers?category=option&baseCoin=ETH
  GET /v5/market/orderbook?category=option&symbol=ETH-27JUN26-3000-C

Плюси проти Deribit: менший мінімальний лот (0.1 ETH) і котирування в USDC.
Мінуси: коротший горизонт експірацій і тонші стакани на далеких страйках.
"""
from __future__ import annotations

import datetime as dt
from typing import Optional

from ...models import Level, OptionChain, OptionQuote, OptionType, OrderBook
from ...util.http import HttpClient
from ..base import OptionsAdapter
from .deribit import parse_symbol

BASE = "https://api.bybit.com"


class BybitAdapter(OptionsAdapter):
    venue = "bybit"

    def __init__(self, cache_dir: Optional[str] = None):
        self.http = HttpClient(BASE, rate_limit_per_sec=8.0, cache_dir=cache_dir)

    def fetch_chain(self, underlying: str, with_books: bool = False) -> OptionChain:
        now = dt.datetime.now(dt.timezone.utc)
        data = self.http.get(
            "/v5/market/tickers", params={"category": "option", "baseCoin": underlying}
        )
        rows = data.get("result", {}).get("list", [])
        quotes: list[OptionQuote] = []
        forwards: dict[dt.datetime, float] = {}
        spot = 0.0
        for row in rows:
            parsed = parse_symbol(row["symbol"])
            if parsed is None:
                continue
            expiry, strike, opt_type = parsed
            fwd = float(row.get("underlyingPrice") or 0.0)
            spot = float(row.get("indexPrice") or spot)
            if fwd:
                forwards[expiry] = fwd
            def f(key):
                v = row.get(key)
                return float(v) if v not in (None, "", "0") else None

            quotes.append(
                OptionQuote(
                    venue=self.venue,
                    symbol=row["symbol"],
                    underlying=underlying,
                    expiry=expiry,
                    strike=strike,
                    opt_type=opt_type,
                    bid=f("bid1Price"),
                    ask=f("ask1Price"),
                    mark=f("markPrice"),
                    iv_bid=f("bid1Iv"),
                    iv_ask=f("ask1Iv"),
                    iv_mark=f("markIv"),
                    delta=f("delta"),
                    open_interest=float(row.get("openInterest") or 0.0),
                    min_qty=0.1,
                    book=OrderBook(
                        bids=[Level(f("bid1Price"), float(row.get("bid1Size") or 0))]
                        if f("bid1Price")
                        else [],
                        asks=[Level(f("ask1Price"), float(row.get("ask1Size") or 0))]
                        if f("ask1Price")
                        else [],
                    ),
                )
            )
        return OptionChain(
            venue=self.venue,
            underlying=underlying,
            spot=spot or (list(forwards.values())[0] if forwards else 0.0),
            fetched_at=now,
            quotes=quotes,
            forwards=forwards,
        )

"""Deribit — основне джерело опціонів на ETH/BTC (найглибший ринок і
найдальші експірації серед CEX).

  GET /api/v2/public/get_instruments?currency=ETH&kind=option&expired=false
  GET /api/v2/public/get_book_summary_by_currency?currency=ETH&kind=option
  GET /api/v2/public/ticker?instrument_name=ETH-26JUN26-3000-C
  GET /api/v2/public/get_order_book?instrument_name=...&depth=10
  GET /api/v2/public/get_index_price?index_name=eth_usd

Ключова пастка: премія котирується В БАЗОВІЙ ВАЛЮТІ (ETH), тому
USD-премія = price * underlying_price. `underlying_price` у відповіді —
це ФОРВАРД відповідної експірації, саме його треба класти в Black-76.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
from typing import Optional

from ...models import Level, OptionChain, OptionQuote, OptionType, OrderBook
from ...util.http import HttpClient
from ..base import OptionsAdapter

log = logging.getLogger(__name__)
BASE = "https://www.deribit.com/api/v2"
_SYM = re.compile(r"^(?P<cur>[A-Z]+)-(?P<exp>\d{1,2}[A-Z]{3}\d{2})-(?P<strike>[\d.]+)-(?P<cp>[CP])$")
_MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}


def parse_symbol(sym: str) -> Optional[tuple[dt.datetime, float, OptionType]]:
    m = _SYM.match(sym)
    if not m:
        return None
    raw = m.group("exp")
    day = int(raw[:-5])
    mon = _MONTHS[raw[-5:-2]]
    year = 2000 + int(raw[-2:])
    # експірація Deribit — 08:00 UTC
    expiry = dt.datetime(year, mon, day, 8, tzinfo=dt.timezone.utc)
    return expiry, float(m.group("strike")), (
        OptionType.CALL if m.group("cp") == "C" else OptionType.PUT
    )


class DeribitAdapter(OptionsAdapter):
    venue = "deribit"

    def __init__(self, testnet: bool = False, cache_dir: Optional[str] = None):
        base = "https://test.deribit.com/api/v2" if testnet else BASE
        self.http = HttpClient(base, rate_limit_per_sec=8.0, cache_dir=cache_dir)

    def _result(self, path: str, params: dict):
        data = self.http.get(path, params=params)
        return data.get("result", data)

    def index_price(self, underlying: str) -> float:
        r = self._result("/public/get_index_price", {"index_name": f"{underlying.lower()}_usd"})
        return float(r["index_price"])

    def fetch_chain(self, underlying: str, with_books: bool = False) -> OptionChain:
        now = dt.datetime.now(dt.timezone.utc)
        spot = self.index_price(underlying)
        rows = self._result(
            "/public/get_book_summary_by_currency", {"currency": underlying, "kind": "option"}
        )
        quotes: list[OptionQuote] = []
        forwards: dict[dt.datetime, float] = {}
        for row in rows:
            parsed = parse_symbol(row["instrument_name"])
            if parsed is None:
                continue
            expiry, strike, opt_type = parsed
            fwd = float(row.get("underlying_price") or spot)
            forwards[expiry] = fwd
            bid = row.get("bid_price")
            ask = row.get("ask_price")
            mark = row.get("mark_price")
            quotes.append(
                OptionQuote(
                    venue=self.venue,
                    symbol=row["instrument_name"],
                    underlying=underlying,
                    expiry=expiry,
                    strike=strike,
                    opt_type=opt_type,
                    # ETH -> USD
                    bid=float(bid) * fwd if bid else None,
                    ask=float(ask) * fwd if ask else None,
                    mark=float(mark) * fwd if mark else None,
                    iv_mark=(float(row["mark_iv"]) / 100.0) if row.get("mark_iv") else None,
                    open_interest=float(row.get("open_interest") or 0.0),
                    contract_size=1.0,
                    min_qty=1.0 if underlying == "BTC" else 1.0,
                )
            )
        chain = OptionChain(
            venue=self.venue,
            underlying=underlying,
            spot=spot,
            fetched_at=now,
            quotes=quotes,
            forwards=forwards,
        )
        if with_books:
            for q in quotes:
                self.load_book(q)
        return chain

    def load_book(self, quote: OptionQuote, depth: int = 10) -> OptionQuote:
        r = self._result(
            "/public/get_order_book", {"instrument_name": quote.symbol, "depth": depth}
        )
        fwd = float(r.get("underlying_price") or quote.strike)
        quote.book = OrderBook(
            bids=[Level(float(p) * fwd, float(s)) for p, s in r.get("bids", [])],
            asks=[Level(float(p) * fwd, float(s)) for p, s in r.get("asks", [])],
            ts=dt.datetime.now(dt.timezone.utc),
        )
        return quote

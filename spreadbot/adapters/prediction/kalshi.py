"""Kalshi: регульований майданчик США.

  GET https://api.elections.kalshi.com/trade-api/v2/markets?limit=&status=open
  GET .../markets/{ticker}/orderbook
Особливості: ціни в центах (1..99), є формула комісії 0.07*C*P*(1-P),
серії KXETH*/KXBTC* мають діапазонні страйки (RANGE_AT_EXPIRY).
Приватні ендпоінти вимагають RSA-підпису запиту (KALSHI-ACCESS-KEY/-SIGNATURE).
"""
from __future__ import annotations

import datetime as dt
from typing import Iterable, Optional

from ...models import Level, OrderBook, PredictionMarket
from ...parsing.claim import parse_title
from ...util.http import HttpClient
from ..base import PredictionAdapter

BASE = "https://api.elections.kalshi.com/trade-api/v2"


class KalshiAdapter(PredictionAdapter):
    venue = "kalshi"

    def __init__(self, cache_dir: Optional[str] = None):
        self.http = HttpClient(BASE, rate_limit_per_sec=5.0, cache_dir=cache_dir)

    def list_markets(
        self,
        assets: Iterable[str] = ("ETH", "BTC"),
        min_days: float = 7.0,
        max_days: float = 1000.0,
        limit: int = 200,
    ) -> list[PredictionMarket]:
        now = dt.datetime.now(dt.timezone.utc)
        data = self.http.get("/markets", params={"limit": min(limit, 200), "status": "open"})
        out: list[PredictionMarket] = []
        for row in data.get("markets", []):
            title = row.get("title") or row.get("subtitle") or ""
            end = row.get("close_time")
            end_dt = dt.datetime.fromisoformat(end.replace("Z", "+00:00")) if end else None
            claim = parse_title(title, end_date=end_dt, resolution_source=row.get("rules_primary", ""))
            if claim is None or claim.asset not in set(assets):
                continue
            days = claim.days_to_deadline(now)
            if not (min_days <= days <= max_days):
                continue
            yes_bid = row.get("yes_bid")
            yes_ask = row.get("yes_ask")
            m = PredictionMarket(
                venue=self.venue,
                market_id=row["ticker"],
                claim=claim,
                volume_usd=float(row.get("volume", 0)),
                open_interest_usd=float(row.get("open_interest", 0)),
                fetched_at=now,
                url=f"https://kalshi.com/markets/{row['ticker']}",
            )
            if yes_bid is not None and yes_ask is not None:
                m.yes_book = OrderBook([Level(yes_bid / 100.0, 0.0)], [Level(yes_ask / 100.0, 0.0)])
                m.no_book = OrderBook(
                    [Level(1 - yes_ask / 100.0, 0.0)], [Level(1 - yes_bid / 100.0, 0.0)]
                )
            out.append(m)
        return out

    def load_books(self, market: PredictionMarket) -> PredictionMarket:
        data = self.http.get(f"/markets/{market.market_id}/orderbook")
        ob = data.get("orderbook", {})
        yes = [Level(p / 100.0, float(s)) for p, s in (ob.get("yes") or [])]
        no = [Level(p / 100.0, float(s)) for p, s in (ob.get("no") or [])]
        # Kalshi віддає лише БІДИ обох сторін; аск YES = 1 - бід NO
        market.yes_book = OrderBook(
            bids=sorted(yes, key=lambda l: l.price, reverse=True),
            asks=sorted([Level(1 - l.price, l.size) for l in no], key=lambda l: l.price),
        )
        market.no_book = OrderBook(
            bids=sorted(no, key=lambda l: l.price, reverse=True),
            asks=sorted([Level(1 - l.price, l.size) for l in yes], key=lambda l: l.price),
        )
        return market

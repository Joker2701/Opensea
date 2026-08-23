"""Polymarket: Gamma API (метадані ринків) + CLOB API (стакани).

Ендпоінти (публічні, без ключа для читання):
  GET https://gamma-api.polymarket.com/markets?closed=false&limit=...
  GET https://clob.polymarket.com/book?token_id=<erc1155 token id>
  GET https://clob.polymarket.com/prices-history?market=<token>&interval=max
WS:   wss://ws-subscriptions-clob.polymarket.com/ws/market  (канал "book")

Важливо для нашої задачі:
  * `outcomes` / `clobTokenIds` приходять РЯДКАМИ з JSON усередині;
  * ціна токена = ймовірність (0..1), розмір = кількість контрактів по $1;
  * правило резолву — у полі `description`; його треба зберігати і
    перевіряти джерело ціни (Binance/Coinbase, 1m high vs close).
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from typing import Iterable, Optional

from ...models import Level, OrderBook, PredictionMarket
from ...parsing.claim import parse_title
from ...util.http import HttpClient
from ..base import PredictionAdapter

log = logging.getLogger(__name__)

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"


def _jsonish(value, default):
    if value is None:
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _parse_ts(value: Optional[str]) -> Optional[dt.datetime]:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class PolymarketAdapter(PredictionAdapter):
    venue = "polymarket"

    def __init__(self, cache_dir: Optional[str] = None, rate_limit: float = 4.0):
        self.gamma = HttpClient(GAMMA, rate_limit_per_sec=rate_limit, cache_dir=cache_dir)
        self.clob = HttpClient(CLOB, rate_limit_per_sec=rate_limit, cache_dir=cache_dir)

    # ------------------------------------------------------------------ #
    def list_markets(
        self,
        assets: Iterable[str] = ("ETH", "BTC"),
        min_days: float = 7.0,
        max_days: float = 1000.0,
        limit: int = 200,
    ) -> list[PredictionMarket]:
        now = dt.datetime.now(dt.timezone.utc)
        out: list[PredictionMarket] = []
        seen = 0
        offset = 0
        page = min(limit, 100)
        while len(out) < limit:
            rows = self.gamma.get(
                "/markets",
                params={
                    "closed": "false",
                    "archived": "false",
                    "active": "true",
                    "limit": page,
                    "offset": offset,
                    "order": "volumeNum",
                    "ascending": "false",
                },
            )
            if not rows:
                break
            offset += page
            seen += len(rows)
            for row in rows:
                m = self._to_market(row, now, assets, min_days, max_days)
                if m is not None:
                    out.append(m)
            if len(rows) < page:
                break
        log.info(
            "polymarket: отримано %d ринків з API, розпізнано і пройшло фільтри %d "
            "(assets=%s, %.0f-%.0f днів) — увімкніть -v/DEBUG, щоб побачити "
            "нерозпізнані заголовки", seen, len(out), sorted(assets), min_days, max_days,
        )
        return out[:limit]

    def _to_market(self, row: dict, now, assets, min_days, max_days) -> Optional[PredictionMarket]:
        title = row.get("question") or row.get("title") or ""
        end = _parse_ts(row.get("endDate") or row.get("end_date_iso"))
        claim = parse_title(
            title,
            end_date=end,
            resolution_source=(row.get("description") or "")[:400] or "unknown",
        )
        if claim is None:
            # діагностика покриття парсера на живих заголовках — regex-и
            # ніколи не перевірялись проти реального фіду, тому carantine
            # без логу зробив би прогалини непомітними
            log.debug("не розпізнано заголовок ринку: %r", title)
            return None
        if claim.asset not in set(assets):
            return None
        days = claim.days_to_deadline(now)
        if not (min_days <= days <= max_days):
            return None

        outcomes = _jsonish(row.get("outcomes"), ["Yes", "No"])
        token_ids = _jsonish(row.get("clobTokenIds"), [])
        prices = [float(p) for p in _jsonish(row.get("outcomePrices"), []) or []]

        market = PredictionMarket(
            venue=self.venue,
            market_id=str(row.get("conditionId") or row.get("id")),
            claim=claim,
            volume_usd=float(row.get("volumeNum") or row.get("volume") or 0.0),
            open_interest_usd=float(row.get("openInterest") or 0.0),
            fetched_at=now,
            url=f"https://polymarket.com/event/{row.get('slug', '')}",
            raw={"tokens": dict(zip([str(o).lower() for o in outcomes], token_ids))},
        )
        # попередні ціни зі списку — щоб фільтрувати ДО важкого запиту стаканів
        if len(prices) >= 2:
            market.yes_book = OrderBook(
                bids=[Level(prices[0], 0.0)], asks=[Level(prices[0], 0.0)]
            )
            market.no_book = OrderBook(
                bids=[Level(prices[1], 0.0)], asks=[Level(prices[1], 0.0)]
            )
        return market

    # ------------------------------------------------------------------ #
    def load_books(self, market: PredictionMarket) -> PredictionMarket:
        tokens = market.raw.get("tokens", {})
        for name, book_attr in (("yes", "yes_book"), ("no", "no_book")):
            tid = tokens.get(name)
            if not tid:
                continue
            data = self.clob.get("/book", params={"token_id": tid})
            setattr(market, book_attr, self._to_book(data))
        return market

    @staticmethod
    def _to_book(data: dict) -> OrderBook:
        def levels(rows, reverse):
            out = [Level(float(r["price"]), float(r["size"])) for r in rows or []]
            return sorted(out, key=lambda l: l.price, reverse=reverse)

        return OrderBook(
            bids=levels(data.get("bids"), reverse=True),
            asks=levels(data.get("asks"), reverse=False),
            ts=dt.datetime.now(dt.timezone.utc),
        )

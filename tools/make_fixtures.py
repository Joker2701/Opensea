#!/usr/bin/env python3
"""Генератор синтетичних фікстур у СХЕМІ реальних API.

Ланцюг опціонів будується з параметричної усмішки, тому він внутрішньо
несуперечливий (put-call parity, зростання дисперсії за терміном) —
на ньому можна перевіряти математику, а не тільки парсинг.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from spreadbot.pricing.bs import black76  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tests", "fixtures")
NOW = dt.datetime(2026, 8, 23, 12, 0, tzinfo=dt.timezone.utc)
SPOT = 2400.0
BASIS = 0.04          # річний керрі форварда (контанго)
MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]


def smile(k: float, t: float) -> float:
    """IV(log-moneyness, термін): ATM ~60%, put-skew, легка усмішка."""
    atm = 0.60 + 0.02 * math.log(max(t, 0.05) / 0.5)
    return max(atm - 0.12 * k + 0.10 * k * k, 0.15)


def build_deribit(path: str) -> None:
    expiries = [
        dt.datetime(2026, 9, 25, 8, tzinfo=dt.timezone.utc),
        dt.datetime(2026, 12, 25, 8, tzinfo=dt.timezone.utc),
        dt.datetime(2027, 3, 26, 8, tzinfo=dt.timezone.utc),
        dt.datetime(2027, 6, 25, 8, tzinfo=dt.timezone.utc),
    ]
    strikes = [1200, 1400, 1600, 1800, 2000, 2200, 2400, 2600, 2800, 3000,
               3200, 3500, 4000, 4500, 5000, 6000]
    rows = []
    for exp in expiries:
        t = (exp - NOW).total_seconds() / (365 * 86400)
        fwd = SPOT * math.exp(BASIS * t)
        for k_abs in strikes:
            k = math.log(k_abs / fwd)
            iv = smile(k, t)
            for cp, is_call in (("C", True), ("P", False)):
                px_usd = black76(fwd, k_abs, t, iv, is_call)
                px = px_usd / fwd                      # Deribit котирує в ETH
                spread = max(px * 0.06, 0.0005)        # реалістичний бід-аск
                rows.append(
                    {
                        "instrument_name": f"ETH-{exp.day}{MONTHS[exp.month-1]}{exp.year%100}-{k_abs}-{cp}",
                        "underlying_price": round(fwd, 2),
                        "bid_price": round(max(px - spread / 2, 0.0), 5),
                        "ask_price": round(px + spread / 2, 5),
                        "mark_price": round(px, 5),
                        "mark_iv": round(iv * 100, 2),
                        "open_interest": 500.0,
                        "volume": 25.0,
                    }
                )
    payload = {
        "/public/get_index_price": {"result": {"index_price": SPOT}},
        "/public/get_book_summary_by_currency": {"result": rows},
    }
    with open(path, "w") as fh:
        json.dump(payload, fh, indent=1)
    print(f"deribit fixture: {len(rows)} інструментів -> {path}")


def book(price: float, depth_usd: float, n: int = 5, tick: float = 0.01) -> dict:
    """Стакан навколо ціни: розмір у контрактах ($1 виплати)."""
    bids = [{"price": round(price - tick * (i + 1), 3), "size": depth_usd * (i + 1)} for i in range(n)]
    asks = [{"price": round(price + tick * i, 3), "size": depth_usd * (i + 1)} for i in range(n)]
    return {"bids": bids, "asks": asks}


def build_polymarket(path: str) -> None:
    markets = [
        {
            "id": "512001",
            "conditionId": "0xeth4k2027",
            "slug": "will-eth-hit-4000-by-june-30-2027",
            "question": "Will Ethereum hit $4,000 by June 30, 2027?",
            "description": (
                "This market resolves YES if the price of Ethereum (ETH) trades at or above "
                "$4,000.00 at any point before June 30, 2027, 23:59 ET, according to the "
                "Binance 1-minute candle high for ETHUSDT."
            ),
            "endDate": "2027-06-30T23:59:00Z",
            "outcomes": "[\"Yes\", \"No\"]",
            # едж навмисно ширший за мінімально можливий: з 1 квітня 2026
            # Polymarket бере комісію на маркет-ноги (до 1,8% від шейрів,
            # див. strategy/costs.py) — тонкий едж 9,5 в.п. з'їдався б нею
            # цілком, тож демо-фікстура тепер показує реалістично привабливу
            # можливість, а не граничний випадок
            "outcomePrices": "[\"0.42\", \"0.58\"]",
            "clobTokenIds": "[\"111111\", \"222222\"]",
            "volumeNum": 4_200_000,
            "openInterest": 900_000,
        },
        {
            "id": "512002",
            "conditionId": "0xeth5k2027",
            "slug": "will-eth-hit-5000-by-june-30-2027",
            "question": "Will Ethereum hit $5,000 by June 30, 2027?",
            "description": "Resolves YES if ETH trades at or above $5,000 before June 30, 2027 (Binance 1m high).",
            "endDate": "2027-06-30T23:59:00Z",
            "outcomes": "[\"Yes\", \"No\"]",
            "outcomePrices": "[\"0.28\", \"0.72\"]",
            "clobTokenIds": "[\"333333\", \"444444\"]",
            "volumeNum": 1_100_000,
            "openInterest": 300_000,
        },
        {
            "id": "512003",
            "conditionId": "0xethabove3k",
            "slug": "will-eth-be-above-3000-on-december-25-2026",
            "question": "Will ETH be above $3,000 on December 25, 2026?",
            "description": "Resolves YES if ETH is above $3,000 at 8:00 UTC on December 25, 2026 (Chainlink ETH/USD).",
            "endDate": "2026-12-25T08:00:00Z",
            "outcomes": "[\"Yes\", \"No\"]",
            "outcomePrices": "[\"0.36\", \"0.64\"]",
            "clobTokenIds": "[\"555555\", \"666666\"]",
            "volumeNum": 800_000,
            "openInterest": 210_000,
        },
    ]
    payload = {
        "/markets": markets,
        "/book?token_id=111111": book(0.425, 4_000),
        "/book?token_id=222222": book(0.575, 4_000),
        "/book?token_id=333333": book(0.285, 2_000),
        "/book?token_id=444444": book(0.715, 2_000),
        "/book?token_id=555555": book(0.365, 3_000),
        "/book?token_id=666666": book(0.635, 3_000),
    }
    with open(path, "w") as fh:
        json.dump(payload, fh, indent=1)
    print(f"polymarket fixture: {len(markets)} ринків -> {path}")


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    build_deribit(os.path.join(OUT, "deribit_eth.json"))
    build_polymarket(os.path.join(OUT, "polymarket_crypto.json"))

"""SQLite-сховище: знімки ринку і знайдені можливості.

Мета — не «журнал заради журналу», а можливість пізніше:
  * порахувати, наскільки едж був справжнім (чи зійшлася модель із фактом);
  * проганяти бектест на реальних історичних знімках, а не на симуляції.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
from typing import Iterable, Optional

from ..models import Opportunity

SCHEMA = """
CREATE TABLE IF NOT EXISTS opportunities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    key TEXT NOT NULL,
    venue_pm TEXT, venue_opt TEXT,
    asset TEXT, claim_kind TEXT, threshold REAL, deadline TEXT,
    market_prob REAL, model_prob REAL, edge_pp REAL,
    capital REAL, worst_pnl REAL, ev_pnl REAL, ev_apr REAL, score REAL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_opp_key_ts ON opportunities(key, ts);

CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    source TEXT NOT NULL,
    asset TEXT,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snap_src_ts ON snapshots(source, ts);

CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    opened_at TEXT NOT NULL,
    key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    capital REAL,
    payload TEXT NOT NULL,
    closed_at TEXT, realized_pnl REAL
);
"""


class Storage:
    def __init__(self, path: str = "spreadbot.sqlite"):
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def save_opportunity(self, op: Opportunity) -> None:
        st, m = op.structure, op.metrics
        claim = st.prediction.market.claim
        payload = {
            "name": st.name,
            "note": st.note,
            "prediction": {
                "venue": st.prediction.market.venue,
                "market_id": st.prediction.market.market_id,
                "outcome": st.prediction.outcome.value,
                "size": st.prediction.size,
                "price": st.prediction.price,
                "url": st.prediction.market.url,
            },
            "options": [
                {
                    "symbol": l.quote.symbol,
                    "venue": l.quote.venue,
                    "side": l.side.value,
                    "qty": l.qty,
                    "price": l.price,
                }
                for l in st.options
            ],
            "metrics": m.__dict__,
        }
        self.conn.execute(
            "INSERT INTO opportunities (ts,key,venue_pm,venue_opt,asset,claim_kind,threshold,"
            "deadline,market_prob,model_prob,edge_pp,capital,worst_pnl,ev_pnl,ev_apr,score,payload)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                op.created_at.isoformat(),
                op.key,
                st.prediction.market.venue,
                st.options[0].quote.venue if st.options else "",
                claim.asset,
                claim.kind.value,
                claim.threshold,
                claim.deadline.isoformat(),
                m.market_prob,
                m.model_prob,
                m.edge_pp,
                m.capital,
                m.worst_pnl,
                m.ev_pnl,
                m.ev_apr,
                op.score,
                json.dumps(payload, default=str),
            ),
        )
        self.conn.commit()

    def save_snapshot(self, source: str, asset: str, payload: dict) -> None:
        self.conn.execute(
            "INSERT INTO snapshots (ts, source, asset, payload) VALUES (?,?,?,?)",
            (
                dt.datetime.now(dt.timezone.utc).isoformat(),
                source,
                asset,
                json.dumps(payload, default=str),
            ),
        )
        self.conn.commit()

    def recent(self, limit: int = 20) -> Iterable[sqlite3.Row]:
        self.conn.row_factory = sqlite3.Row
        return self.conn.execute(
            "SELECT * FROM opportunities ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()

    def close(self) -> None:
        self.conn.close()

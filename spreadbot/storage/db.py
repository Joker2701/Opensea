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
    chat_id TEXT NOT NULL,
    key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    asset TEXT, claim_kind TEXT, threshold REAL, deadline TEXT,
    capital REAL,
    payload TEXT NOT NULL,
    closed_at TEXT, realized_pnl REAL
);
CREATE INDEX IF NOT EXISTS idx_pos_status ON positions(status, chat_id);

-- Налаштування Telegram-бота і його стан (керування з телефону).
CREATE TABLE IF NOT EXISTS bot_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Дедуп алертів: той самий сигнал (ключ можливості) не шлеться повторно
-- в межах кулдауну, і це переживає перезапуск бота.
CREATE TABLE IF NOT EXISTS sent_alerts (
    key TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    PRIMARY KEY (key, chat_id)
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

    # ------------------------------------------------------------------ #
    # Стан і налаштування Telegram-бота
    # ------------------------------------------------------------------ #
    def get_state(self, key: str) -> Optional[str]:
        row = self.conn.execute(
            "SELECT value FROM bot_state WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row else None

    def set_state(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO bot_state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.conn.commit()

    def get_owner_chat_id(self) -> Optional[str]:
        return self.get_state("owner_chat_id")

    def set_owner_chat_id(self, chat_id: str) -> None:
        self.set_state("owner_chat_id", str(chat_id))

    def get_settings(self) -> dict:
        from .settings_schema import DEFAULT_BOT_SETTINGS  # локальний імпорт: без циклів

        raw = self.get_state("settings")
        settings = dict(DEFAULT_BOT_SETTINGS)
        if raw:
            try:
                settings.update(json.loads(raw))
            except (json.JSONDecodeError, TypeError):
                pass
        return settings

    def save_settings(self, settings: dict) -> None:
        self.set_state("settings", json.dumps(settings, default=str))

    def update_settings(self, **changes) -> dict:
        settings = self.get_settings()
        settings.update(changes)
        self.save_settings(settings)
        return settings

    # ------------------------------------------------------------------ #
    # Дедуп алертів (переживає перезапуск)
    # ------------------------------------------------------------------ #
    def was_alerted_recently(self, key: str, chat_id: str, cooldown_minutes: float) -> bool:
        row = self.conn.execute(
            "SELECT ts FROM sent_alerts WHERE key = ? AND chat_id = ?", (key, chat_id)
        ).fetchone()
        if not row:
            return False
        last = dt.datetime.fromisoformat(row[0])
        age_min = (dt.datetime.now(dt.timezone.utc) - last).total_seconds() / 60.0
        return age_min < cooldown_minutes

    def mark_alerted(self, key: str, chat_id: str) -> None:
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        self.conn.execute(
            "INSERT INTO sent_alerts (key, chat_id, ts) VALUES (?, ?, ?) "
            "ON CONFLICT(key, chat_id) DO UPDATE SET ts = excluded.ts",
            (key, chat_id, now),
        )
        self.conn.commit()

    # ------------------------------------------------------------------ #
    # Позиції: користувач підтвердив «я взяв це» -> бот пам'ятає і стежить
    # ------------------------------------------------------------------ #
    def open_position(
        self,
        chat_id: str,
        key: str,
        asset: str,
        claim_kind: str,
        threshold: float,
        deadline: str,
        capital: float,
        payload: dict,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO positions (opened_at,chat_id,key,status,asset,claim_kind,"
            "threshold,deadline,capital,payload) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                dt.datetime.now(dt.timezone.utc).isoformat(),
                chat_id, key, "open", asset, claim_kind, threshold, deadline,
                capital, json.dumps(payload, default=str),
            ),
        )
        self.conn.commit()
        return cur.lastrowid

    def list_open_positions(self, chat_id: Optional[str] = None) -> list[sqlite3.Row]:
        self.conn.row_factory = sqlite3.Row
        if chat_id is None:
            return self.conn.execute(
                "SELECT * FROM positions WHERE status='open' ORDER BY id"
            ).fetchall()
        return self.conn.execute(
            "SELECT * FROM positions WHERE status='open' AND chat_id=? ORDER BY id",
            (chat_id,),
        ).fetchall()

    def get_position(self, position_id: int) -> Optional[sqlite3.Row]:
        self.conn.row_factory = sqlite3.Row
        return self.conn.execute(
            "SELECT * FROM positions WHERE id=?", (position_id,)
        ).fetchone()

    def close_position(self, position_id: int, realized_pnl: Optional[float] = None) -> None:
        self.conn.execute(
            "UPDATE positions SET status='closed', closed_at=?, realized_pnl=? WHERE id=?",
            (dt.datetime.now(dt.timezone.utc).isoformat(), realized_pnl, position_id),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

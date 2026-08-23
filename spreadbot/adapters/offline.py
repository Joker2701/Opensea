"""Офлайн-режим: ті самі адаптери, але HTTP підмінений файлами-фікстурами.

Навіщо: (1) детерміновані тести парсингу і математики; (2) робота там, де
біржі недоступні (наш CI/пісочниця); (3) «сухий прогін» стратегії на
збережених знімках ринку без ризику зловити рейт-ліміт.
"""
from __future__ import annotations

import json
import os
from typing import Any, Optional

from .options.deribit import DeribitAdapter
from .prediction.polymarket import PolymarketAdapter


class FileHttpClient:
    """Мінімальна заміна HttpClient: віддає збережені відповіді за шляхом."""

    def __init__(self, path: str):
        with open(path) as fh:
            self.data: dict[str, Any] = json.load(fh)
        self.path = path

    def get(self, path: str, params: Optional[dict] = None) -> Any:
        key = path
        if params:
            for name in ("token_id", "instrument_name", "index_name"):
                if name in params:
                    scoped = f"{path}?{name}={params[name]}"
                    if scoped in self.data:
                        return self.data[scoped]
        if key not in self.data:
            raise KeyError(f"немає фікстури для {key} у {self.path}")
        return self.data[key]


def offline_deribit(fixture: str) -> DeribitAdapter:
    a = DeribitAdapter()
    a.http = FileHttpClient(fixture)  # type: ignore[assignment]
    return a


def offline_polymarket(fixture: str) -> PolymarketAdapter:
    a = PolymarketAdapter()
    a.gamma = FileHttpClient(fixture)  # type: ignore[assignment]
    a.clob = FileHttpClient(fixture)   # type: ignore[assignment]
    return a


FIXTURE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "tests", "fixtures")

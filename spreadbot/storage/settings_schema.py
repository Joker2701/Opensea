"""Схема налаштувань, які можна крутити з Telegram.

Один запис на всю БД (бот — особистий, власник один). Значення живуть у
`bot_state` як JSON і накладаються поверх `config.yaml` перед кожним
сканування — тобто конфіг-файл задає стартові дефолти, а Telegram їх
перекриває "на льоту", без перезапуску процесу.
"""
from __future__ import annotations

DEFAULT_BOT_SETTINGS = {
    "paused": False,
    "capital_usd": 10_000.0,
    "min_edge_pp": 3.0,
    "min_net_edge_pp": 1.0,
    "assets": ["ETH", "BTC"],
    "prediction_venues": ["polymarket"],
    "option_venues": ["bybit"],
    "scan_interval_min": 15.0,
}

#: крок зміни для кожного числового поля в меню (+/- кнопки)
STEP = {
    "capital_usd": 1000.0,
    "min_edge_pp": 0.5,
    "min_net_edge_pp": 0.5,
    "scan_interval_min": 5.0,
}

#: (мін, макс) — щоб кнопками не загнати налаштування в абсурд
BOUNDS = {
    "capital_usd": (100.0, 1_000_000.0),
    "min_edge_pp": (0.0, 50.0),
    "min_net_edge_pp": (0.0, 50.0),
    "scan_interval_min": (5.0, 240.0),
}

ALL_ASSETS = ["ETH", "BTC"]
ALL_PREDICTION_VENUES = ["polymarket", "kalshi"]
ALL_OPTION_VENUES = ["bybit", "deribit"]


def clamp(field: str, value: float) -> float:
    lo, hi = BOUNDS.get(field, (float("-inf"), float("inf")))
    return max(lo, min(hi, value))

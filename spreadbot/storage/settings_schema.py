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
    "max_days": 365.0,           # максимальний термін до дедлайну ставки
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
    "max_days": 30.0,
    "scan_interval_min": 5.0,
}

#: (мін, макс) — щоб кнопками не загнати налаштування в абсурд
BOUNDS = {
    "capital_usd": (100.0, 1_000_000.0),
    "min_edge_pp": (0.0, 50.0),
    "min_net_edge_pp": (0.0, 50.0),
    "max_days": (7.0, 1500.0),
    "scan_interval_min": (5.0, 240.0),
}

ALL_ASSETS = ["ETH", "BTC"]

#: Платформи, які бот УМІЄ сканувати (для кожної написаний окремий адаптер
#: у spreadbot/adapters/ — "додати платформу з телеграму" без свого адаптера
#: технічно неможливо, у кожної свій формат API). Меню в Telegram лише
#: вмикає/вимикає платформи З ЦЬОГО списку. Щоб додати нову платформу —
#: пишеться адаптер (adapters/prediction/… або adapters/options/…) і назва
#: дописується сюди; після цього вона одразу з'являється в /menu.
ALL_PREDICTION_VENUES = ["polymarket", "kalshi"]
ALL_OPTION_VENUES = ["bybit", "deribit"]


def clamp(field: str, value: float) -> float:
    lo, hi = BOUNDS.get(field, (float("-inf"), float("inf")))
    return max(lo, min(hi, value))

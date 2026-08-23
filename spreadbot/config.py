"""Конфіг бота (YAML -> дата-класи). Дефолти консервативні."""
from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass, field
from typing import Any, Optional

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore

from .strategy.sizing import SizingConfig


@dataclass
class ScanConfig:
    assets: list[str] = field(default_factory=lambda: ["ETH", "BTC"])
    prediction_venues: list[str] = field(default_factory=lambda: ["polymarket"])
    option_venues: list[str] = field(default_factory=lambda: ["deribit"])
    min_days: float = 21.0
    max_days: float = 365.0             # дефолт: до року; змінюється з /menu
    min_volume_usd: float = 100_000.0
    min_edge_pp: float = 3.0            # мінімальний «сирий» едж до розміщення
    min_net_edge_pp: float = 1.0        # після комісій і спреду
    max_markets: int = 200
    strike_ratios: list[float] = field(default_factory=lambda: [0.9, 0.95, 1.0, 1.05, 1.1, 1.2])
    # Спред (довгий+короткий опціон) вимкнено за замовчуванням: коротка нога
    # має власну часову вартість, і при РАННЬОМУ торканні бар'єра (коли до
    # експірації ще далеко) її відкуп може коштувати дорожче, ніж дає
    # інтринсик на порозі — тобто саме та властивість «мінус не більше
    # комісії», яку гарантує одинарний довгий опціон, для спреду не
    # тримається. Вмикайте свідомо, якщо розумієте цей ризик.
    include_spreads: bool = False
    scenario_points: int = 60
    top_n: int = 15


@dataclass
class RiskConfig:
    max_capital_per_trade_usd: float = 5_000.0
    max_total_capital_usd: float = 25_000.0
    risk_measure: str = "cvar"          # "cvar" (рекомендовано) або "worst"
    cvar_alpha: float = 0.05
    # Дві умови стратегії (strategy/fork.py) — необхідна, але НЕ достатня
    # перевірка: вони рахують payoff опціона на порозі за чистим інтринсиком,
    # без комісії на вихід і без того, ЯКОГО ДНЯ ціна туди дійшла. Тому їх
    # виконання саме по собі ще не гарантує невід'ємний результат.
    require_fork: bool = True           # обидві умови вилки — обов'язкові
    max_loss_frac: float = 0.0          # 0.0 = вимкнено; замінено жорсткішим min_worst_return нижче
    exit_policy: str = "unwind"         # "unwind" (розхедж при торканні) або "hold"
    # Комісія + сліпедж на достроковий вихід опціона — реальна вартість,
    # яку треба заплатити, продаючи хедж у момент, коли ціна дійшла до
    # порогу. Тримайте це реалістичною оцінкою round-trip комісії біржі.
    unwind_cost_frac: float = 0.01
    # СПРАВЖНІЙ жорсткий гейт: найгірший сценарій по ВСЬОМУ сценарному
    # розподілу (з комісіями входу і виходу, для будь-якого моменту
    # торкання порогу) має бути НЕ МЕНШЕ цього значення. 0.0 = у грошах
    # результат ніколи не йде в мінус — солвер просто не пропонує
    # конструкцію, для якої це не так, замість того щоб показати її з
    # позначкою "трохи мінус". Це головний практичний гейт бота.
    min_worst_return: Optional[float] = 0.0
    allow_expiry_gap_days: float = 30.0     # наскільки опціон може гаснути раніше за ставку
    require_known_resolution_source: bool = True
    blocked_resolution_keywords: list[str] = field(
        default_factory=lambda: ["UMA discretion", "subjective", "TBD"]
    )
    vol_stress_pp: float = 10.0             # стрес-тест: IV +/- 10 в.п.


@dataclass
class NotifyConfig:
    console: bool = True
    telegram_token_env: str = "SPREADBOT_TG_TOKEN"
    #: якщо чат власника ще не закріплений у БД, перший, хто напише /start,
    #: стає власником. Можна закріпити наперед через цю змінну оточення.
    telegram_chat_env: str = "SPREADBOT_TG_CHAT"
    cooldown_minutes: float = 120.0     # не слати той самий сигнал частіше
    min_score_to_alert: float = 1.0
    scan_interval_min: float = 15.0     # як часто `spreadbot bot` сканує ринки


@dataclass
class Config:
    scan: ScanConfig = field(default_factory=ScanConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    sizing: SizingConfig = field(default_factory=SizingConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    offline: bool = False
    fixtures: dict[str, str] = field(default_factory=dict)
    db_path: str = "spreadbot.sqlite"
    cache_dir: Optional[str] = None
    rate_usd: float = 0.04              # безризикова ставка для дисконтування
    log_level: str = "INFO"

    def __post_init__(self) -> None:
        self._sync_risk_into_sizing()

    def _sync_risk_into_sizing(self) -> None:
        """`risk.*` — єдине джерело правди для лімітів солвера.

        Раніше це синхронізувалось лише всередині `load()` для YAML-гілки,
        тому будь-який прямий `Config()` (демо, тести, програмне використання)
        мовчки брав власні, неузгоджені дефолти `SizingConfig`. Тепер синк
        робиться завжди, одразу після конструювання.
        """
        self.sizing.risk_measure = self.risk.risk_measure
        self.sizing.cvar_alpha = self.risk.cvar_alpha
        self.sizing.max_loss_frac = self.risk.max_loss_frac
        self.sizing.require_fork = self.risk.require_fork
        self.sizing.exit_policy = self.risk.exit_policy
        self.sizing.unwind_cost_frac = self.risk.unwind_cost_frac
        self.sizing.min_worst_return = self.risk.min_worst_return
        self.sizing.capital_usd = min(self.sizing.capital_usd, self.risk.max_capital_per_trade_usd)

    # ------------------------------------------------------------------ #
    @staticmethod
    def _fill(cls, data: dict) -> Any:
        kwargs = {}
        names = {f.name for f in dataclasses.fields(cls)}
        for k, v in (data or {}).items():
            if k in names:
                kwargs[k] = v
        return cls(**kwargs)

    @classmethod
    def load(cls, path: Optional[str] = None) -> "Config":
        if not path or not os.path.exists(path):
            return cls()
        if yaml is None:
            raise RuntimeError("для YAML-конфігу потрібен pyyaml")
        with open(path) as fh:
            raw = yaml.safe_load(fh) or {}
        cfg = cls(
            scan=cls._fill(ScanConfig, raw.get("scan", {})),
            risk=cls._fill(RiskConfig, raw.get("risk", {})),
            sizing=cls._fill(SizingConfig, raw.get("sizing", {})),
            notify=cls._fill(NotifyConfig, raw.get("notify", {})),
        )
        for key in ("offline", "fixtures", "db_path", "cache_dir", "rate_usd", "log_level"):
            if key in raw:
                setattr(cfg, key, raw[key])
        cfg._sync_risk_into_sizing()   # risk.* лишається єдиним джерелом правди
        return cfg

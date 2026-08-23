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
    max_days: float = 900.0
    min_volume_usd: float = 100_000.0
    min_edge_pp: float = 3.0            # мінімальний «сирий» едж до розміщення
    min_net_edge_pp: float = 1.0        # після комісій і спреду
    max_markets: int = 200
    strike_ratios: list[float] = field(default_factory=lambda: [0.9, 0.95, 1.0, 1.05, 1.1, 1.2])
    include_spreads: bool = True
    scenario_points: int = 60
    top_n: int = 15


@dataclass
class RiskConfig:
    max_capital_per_trade_usd: float = 5_000.0
    max_total_capital_usd: float = 25_000.0
    risk_measure: str = "cvar"          # "cvar" (рекомендовано) або "worst"
    cvar_alpha: float = 0.05
    max_loss_frac: float = 0.15         # ліміт на обрану міру ризику
    require_fork: bool = True           # обидві умови вилки — обов'язкові
    exit_policy: str = "unwind"         # "unwind" (розхедж при торканні) або "hold"
    unwind_cost_frac: float = 0.04
    min_worst_return: Optional[float] = None
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
    telegram_chat_env: str = "SPREADBOT_TG_CHAT"
    cooldown_minutes: float = 120.0
    min_score_to_alert: float = 1.0


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
        # ризик-ліміти мають пріоритет над сайзингом
        cfg.sizing.risk_measure = cfg.risk.risk_measure
        cfg.sizing.cvar_alpha = cfg.risk.cvar_alpha
        cfg.sizing.max_loss_frac = cfg.risk.max_loss_frac
        cfg.sizing.require_fork = cfg.risk.require_fork
        cfg.sizing.exit_policy = cfg.risk.exit_policy
        cfg.sizing.unwind_cost_frac = cfg.risk.unwind_cost_frac
        cfg.sizing.min_worst_return = cfg.risk.min_worst_return
        cfg.sizing.capital_usd = min(cfg.sizing.capital_usd, cfg.risk.max_capital_per_trade_usd)
        return cfg

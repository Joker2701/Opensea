"""Контракти адаптерів. Додати нову платформу = реалізувати один клас."""
from __future__ import annotations

import abc
import datetime as dt
from typing import Iterable, Optional

from ..models import OptionChain, PredictionMarket


class PredictionAdapter(abc.ABC):
    venue: str = "generic"
    #: чи повертає майданчик повний стакан (інакше — лише best bid/ask)
    has_orderbook: bool = True

    @abc.abstractmethod
    def list_markets(
        self,
        assets: Iterable[str] = ("ETH", "BTC"),
        min_days: float = 7.0,
        max_days: float = 1000.0,
        limit: int = 200,
    ) -> list[PredictionMarket]:
        ...

    @abc.abstractmethod
    def load_books(self, market: PredictionMarket) -> PredictionMarket:
        """Догрузити стакани для конкретного ринку (окремий запит на біржах,
        які не віддають глибину в списку)."""


class OptionsAdapter(abc.ABC):
    venue: str = "generic"

    @abc.abstractmethod
    def fetch_chain(self, underlying: str, with_books: bool = False) -> OptionChain:
        ...

    def max_expiry(self, chain: OptionChain) -> Optional[dt.datetime]:
        exps = chain.expiries()
        return exps[-1] if exps else None

"""Телеграм-алерти з антиспамом (кулдаун на ключ можливості)."""
from __future__ import annotations

import logging
import os
import time
from typing import Optional

from ..models import Opportunity

log = logging.getLogger(__name__)


class TelegramNotifier:
    name = "telegram"

    def __init__(
        self,
        token_env: str = "SPREADBOT_TG_TOKEN",
        chat_env: str = "SPREADBOT_TG_CHAT",
        cooldown_minutes: float = 120.0,
    ):
        self.token = os.getenv(token_env)
        self.chat = os.getenv(chat_env)
        self.cooldown = cooldown_minutes * 60
        self._sent: dict[str, float] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat)

    def format(self, op: Opportunity) -> str:
        m, st = op.metrics, op.structure
        claim = st.prediction.market.claim
        legs = "\n".join(
            f"• {l.side.value} {l.qty:g} {l.quote.symbol} @ {l.price:,.2f}" for l in st.options
        )
        return (
            f"<b>{st.name}</b>  (score {op.score:.2f})\n"
            f"{claim.raw_title}\n"
            f"едж {m.edge_pp:+.1f} в.п. | ринок {m.market_prob:.1%} vs модель {m.model_prob:.1%}\n"
            f"• {st.prediction.outcome.value.upper()} {st.prediction.size:,.0f} @ "
            f"{st.prediction.price:.3f}\n{legs}\n"
            f"капітал {m.capital:,.0f} | найгірше {m.worst_return:+.1%} | "
            f"очік. {m.ev_apr:+.0%} річних\n"
            f"{st.prediction.market.url}"
        )

    def send(self, op: Opportunity) -> None:
        if not self.enabled:
            log.debug("telegram вимкнено (немає токена/чату)")
            return
        now = time.time()
        if now - self._sent.get(op.key, 0) < self.cooldown:
            return
        try:
            import requests

            requests.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json={
                    "chat_id": self.chat,
                    "text": self.format(op),
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=10,
            )
            self._sent[op.key] = now
        except Exception as exc:  # noqa: BLE001
            log.warning("telegram: %s", exc)

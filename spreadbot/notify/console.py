from __future__ import annotations

from ..models import Opportunity
from ..report import opportunity_card


class ConsoleNotifier:
    name = "console"

    def send(self, op: Opportunity) -> None:
        print(opportunity_card(op))

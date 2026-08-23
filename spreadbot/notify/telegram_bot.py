"""Telegram-бот: сканує ринки і шле алерти, керується з телефону.

Ніякого виконання угод — бот тільки читає ринки і повідомляє. Довгий
полінг (`getUpdates`), без сторонніх бібліотек — тільки `requests`.

Власник один: перший, хто напише /start (або чат, закріплений заздалегідь
через змінну оточення `SPREADBOT_TG_CHAT`), стає власником і зберігається
в SQLite. Усі команди від інших чатів ігноруються.

Налаштування (капітал, поріг едж, які біржі, пауза) зберігаються в БД і
керуються інлайн-кнопками — змінюються "на льоту", без перезапуску.
"""
from __future__ import annotations

import copy
import html
import logging
import time
from typing import Optional

from ..config import Config
from ..models import Opportunity
from ..report import opportunity_card
from ..scanner.scanner import Scanner
from ..storage.db import Storage
from ..storage.settings_schema import (
    ALL_OPTION_VENUES,
    ALL_PREDICTION_VENUES,
    BOUNDS,
    STEP,
    clamp,
)
from ..util.http import HttpClient

log = logging.getLogger(__name__)

API = "https://api.telegram.org"


def _label(field: str) -> str:
    return {
        "capital_usd": "Капітал",
        "min_edge_pp": "Мін. едж",
        "min_net_edge_pp": "Мін. едж (чистий)",
        "max_days": "Термін до дедлайну, макс.",
        "scan_interval_min": "Інтервал сканування",
    }[field]


def _fmt_value(field: str, value: float) -> str:
    if field == "capital_usd":
        return f"${value:,.0f}"
    if field == "scan_interval_min":
        return f"{value:.0f} хв"
    if field == "max_days":
        return f"{value:.0f} дн."
    return f"{value:.1f} п.п."


class TelegramBot:
    def __init__(self, token: str, storage: Storage, cfg: Config):
        # timeout HTTP-клієнта > довжини довгого полінгу (getUpdates timeout=25),
        # інакше запит обривається клієнтом раніше, ніж Telegram встигне відповісти
        self.http = HttpClient(f"{API}/bot{token}", timeout=35.0, rate_limit_per_sec=25.0, max_retries=1)
        self.store = storage
        self.cfg = cfg
        self._last_scan_ts = 0.0
        self._last_ops: list[Opportunity] = []

    # ------------------------------------------------------------------ #
    # Низькорівневий Telegram API
    # ------------------------------------------------------------------ #
    def _call(self, method: str, **params) -> dict:
        return self.http.get(f"/{method}", params=params)

    def send(self, chat_id: str, text: str, keyboard: Optional[list] = None) -> None:
        params = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True}
        if keyboard is not None:
            import json

            params["reply_markup"] = json.dumps({"inline_keyboard": keyboard})
        try:
            self._call("sendMessage", **params)
        except Exception as exc:  # noqa: BLE001
            log.warning("sendMessage: %s", exc)

    def _answer_callback(self, callback_id: str, text: str = "") -> None:
        try:
            self._call("answerCallbackQuery", callback_query_id=callback_id, text=text)
        except Exception as exc:  # noqa: BLE001
            log.debug("answerCallbackQuery: %s", exc)

    def _edit_menu(self, chat_id: str, message_id: int) -> None:
        settings = self.store.get_settings()
        import json

        try:
            self._call(
                "editMessageText",
                chat_id=chat_id,
                message_id=message_id,
                text=self._menu_text(settings),
                parse_mode="HTML",
                reply_markup=json.dumps({"inline_keyboard": self._menu_keyboard(settings)}),
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("editMessageText: %s", exc)

    # ------------------------------------------------------------------ #
    # Меню налаштувань
    # ------------------------------------------------------------------ #
    def _menu_text(self, s: dict) -> str:
        state = "⏸ на паузі" if s["paused"] else "▶️ сканує"
        return (
            "<b>spreadbot — керування</b>\n"
            f"Статус: {state}\n\n"
            f"Капітал на угоду: {_fmt_value('capital_usd', s['capital_usd'])}\n"
            f"Мін. едж (сирий): {_fmt_value('min_edge_pp', s['min_edge_pp'])}\n"
            f"Мін. едж (чистий): {_fmt_value('min_net_edge_pp', s['min_net_edge_pp'])}\n"
            f"Термін до дедлайну, макс.: {_fmt_value('max_days', s['max_days'])}\n"
            f"Інтервал сканування: {_fmt_value('scan_interval_min', s['scan_interval_min'])}\n"
            f"Активи: {', '.join(s['assets']) or '—'}\n"
            f"Предикт-маркети: {', '.join(s['prediction_venues']) or '—'}\n"
            f"Біржі опціонів: {', '.join(s['option_venues']) or '—'}\n"
        )

    def _numeric_row(self, field: str, settings: dict) -> list:
        step = STEP[field]
        return [
            {"text": "−", "callback_data": f"adj:{field}:{-step}"},
            {"text": f"{_label(field)}: {_fmt_value(field, settings[field])}",
             "callback_data": "noop"},
            {"text": "+", "callback_data": f"adj:{field}:{step}"},
        ]

    def _toggle_row(self, group: str, options: list[str], active: list[str]) -> list:
        row = []
        for opt in options:
            mark = "✅" if opt in active else "⬜"
            row.append({"text": f"{mark} {opt}", "callback_data": f"toggle:{group}:{opt}"})
        return row

    def _menu_keyboard(self, s: dict) -> list:
        pause_btn = (
            {"text": "▶️ Відновити", "callback_data": "pause:0"}
            if s["paused"]
            else {"text": "⏸ Пауза", "callback_data": "pause:1"}
        )
        return [
            self._numeric_row("capital_usd", s),
            self._numeric_row("min_edge_pp", s),
            self._numeric_row("min_net_edge_pp", s),
            self._numeric_row("max_days", s),
            self._numeric_row("scan_interval_min", s),
            self._toggle_row("asset", ["ETH", "BTC"], s["assets"]),
            self._toggle_row("pm", ALL_PREDICTION_VENUES, s["prediction_venues"]),
            self._toggle_row("opt", ALL_OPTION_VENUES, s["option_venues"]),
            [pause_btn, {"text": "🔄 Сканувати зараз", "callback_data": "scan:now"}],
            [{"text": "❌ Закрити", "callback_data": "close"}],
        ]

    # ------------------------------------------------------------------ #
    # Обробка вхідних апдейтів
    # ------------------------------------------------------------------ #
    def _is_owner(self, chat_id: str) -> bool:
        owner = self.store.get_owner_chat_id()
        return owner is not None and str(owner) == str(chat_id)

    def _claim_ownership_if_free(self, chat_id: str) -> bool:
        if self.store.get_owner_chat_id() is None:
            self.store.set_owner_chat_id(chat_id)
            log.info("власником бота закріплено чат %s", chat_id)
            return True
        return self._is_owner(chat_id)

    def handle_update(self, update: dict) -> None:
        if "callback_query" in update:
            self._handle_callback(update["callback_query"])
            return
        msg = update.get("message")
        if not msg or "text" not in msg:
            return
        chat_id = str(msg["chat"]["id"])
        text = msg["text"].strip()

        if text.startswith("/start"):
            if not self._claim_ownership_if_free(chat_id):
                return
            self.send(
                chat_id,
                "Привіт! Я шукаю вилки опціон × предикт-маркет і рахую дві умови "
                "стратегії (A і B) у доларах. Ніяких угод сам не відкриваю — "
                "тільки сигналю.\n\n/menu — керування\n/scan — сканувати зараз\n"
                "/list — останні знахідки\n/status — стан",
            )
            self._send_menu(chat_id)
            return

        if not self._is_owner(chat_id):
            return  # чужі чати ігноруємо мовчки

        if text.startswith("/menu") or text.startswith("/settings"):
            self._send_menu(chat_id)
        elif text.startswith("/status"):
            self._cmd_status(chat_id)
        elif text.startswith("/scan"):
            self._run_scan_and_alert(chat_id, manual=True)
        elif text.startswith("/list"):
            self._cmd_list(chat_id)
        elif text.startswith("/pause"):
            self.store.update_settings(paused=True)
            self.send(chat_id, "На паузі. /resume — відновити.")
        elif text.startswith("/resume"):
            self.store.update_settings(paused=False)
            self.send(chat_id, "Відновлено сканування.")
        elif text.startswith("/help"):
            self.send(
                chat_id,
                "/menu — налаштування\n/scan — сканувати зараз\n"
                "/list — останні знахідки\n/status — стан\n"
                "/pause, /resume — призупинити чи відновити",
            )

    def _send_menu(self, chat_id: str) -> None:
        s = self.store.get_settings()
        self.send(chat_id, self._menu_text(s), self._menu_keyboard(s))

    def _handle_callback(self, cq: dict) -> None:
        chat_id = str(cq["message"]["chat"]["id"])
        message_id = cq["message"]["message_id"]
        data = cq.get("data", "")
        self._answer_callback(cq["id"])
        if not self._is_owner(chat_id):
            return

        if data == "noop":
            return
        if data == "close":
            try:
                self._call("deleteMessage", chat_id=chat_id, message_id=message_id)
            except Exception:  # noqa: BLE001
                pass
            return
        if data == "scan:now":
            self._run_scan_and_alert(chat_id, manual=True)
            return
        if data.startswith("pause:"):
            self.store.update_settings(paused=data.endswith(":1"))
        elif data.startswith("adj:"):
            _, field, delta = data.split(":")
            s = self.store.get_settings()
            s[field] = clamp(field, s[field] + float(delta))
            self.store.save_settings(s)
        elif data.startswith("toggle:"):
            _, group, value = data.split(":")
            key = {"asset": "assets", "pm": "prediction_venues", "opt": "option_venues"}[group]
            s = self.store.get_settings()
            cur = list(s[key])
            if value in cur:
                cur.remove(value)
            else:
                cur.append(value)
            s[key] = cur
            self.store.save_settings(s)

        self._edit_menu(chat_id, message_id)

    # ------------------------------------------------------------------ #
    # Команди-звіти
    # ------------------------------------------------------------------ #
    def _cmd_status(self, chat_id: str) -> None:
        s = self.store.get_settings()
        ago = "ще не сканував" if self._last_scan_ts == 0 else f"{(time.time() - self._last_scan_ts) / 60:.0f} хв тому"
        self.send(
            chat_id,
            f"Статус: {'⏸ на паузі' if s['paused'] else '▶️ активний'}\n"
            f"Останнє сканування: {ago}\n"
            f"Знахідок минулого разу: {len(self._last_ops)}",
        )

    def _cmd_list(self, chat_id: str) -> None:
        if not self._last_ops:
            self.send(chat_id, "Поки що порожньо — спробуйте /scan.")
            return
        lines = []
        for i, op in enumerate(self._last_ops[:10], start=1):
            m = op.metrics
            lines.append(
                f"{i}. {op.structure.name} — едж {m.edge_pp:+.1f}п.п., "
                f"очік. {m.ev_apr:+.0%} річних, капітал ${m.capital:,.0f}"
            )
        self.send(chat_id, "<pre>" + html.escape("\n".join(lines)) + "</pre>")

    # ------------------------------------------------------------------ #
    # Сканування
    # ------------------------------------------------------------------ #
    def _build_scan_config(self, settings: dict) -> Config:
        cfg = copy.deepcopy(self.cfg)
        cfg.scan.assets = list(settings["assets"]) or cfg.scan.assets
        cfg.scan.prediction_venues = list(settings["prediction_venues"]) or cfg.scan.prediction_venues
        cfg.scan.option_venues = list(settings["option_venues"]) or cfg.scan.option_venues
        cfg.scan.min_edge_pp = settings["min_edge_pp"]
        cfg.scan.min_net_edge_pp = settings["min_net_edge_pp"]
        cfg.scan.max_days = settings["max_days"]
        cfg.risk.max_capital_per_trade_usd = settings["capital_usd"]
        cfg.sizing.capital_usd = settings["capital_usd"]
        cfg._sync_risk_into_sizing()
        return cfg

    def _run_scan_and_alert(self, chat_id: str, manual: bool = False) -> None:
        settings = self.store.get_settings()
        if settings["paused"] and not manual:
            return
        cfg = self._build_scan_config(settings)
        from ..cli import _adapters  # локальний імпорт: уникаємо циклу cli<->notify

        try:
            pred, opts = _adapters(cfg)
            ops = Scanner(pred, opts, cfg).run()
        except Exception as exc:  # noqa: BLE001
            log.exception("сканування впало")
            if manual:
                self.send(chat_id, f"Помилка сканування: {exc}")
            return

        self._last_ops = ops
        self._last_scan_ts = time.time()

        if not ops:
            if manual:
                self.send(chat_id, "Наразі конструкцій, що проходять умови A/B, немає.")
            return

        new_count = 0
        for op in ops:
            if not manual and self.store.was_alerted_recently(
                op.key, chat_id, self.cfg.notify.cooldown_minutes
            ):
                continue
            self.send(chat_id, f"<pre>{html.escape(opportunity_card(op))}</pre>")
            self.store.mark_alerted(op.key, chat_id)
            new_count += 1

        if manual and new_count == 0:
            self.send(chat_id, "Нових конструкцій немає (усі під кулдауном).")

    # ------------------------------------------------------------------ #
    # Головний цикл
    # ------------------------------------------------------------------ #
    def run_forever(self) -> None:
        preset = None
        if not self.store.get_owner_chat_id():
            preset = self.cfg.notify.telegram_chat_env
        offset: Optional[int] = None
        log.info("Telegram-бот запущено, чекаю на /start...")
        while True:
            try:
                updates = self._call("getUpdates", timeout=25, offset=offset) or {}
                for u in updates.get("result", []):
                    offset = u["update_id"] + 1
                    self.handle_update(u)
            except Exception as exc:  # noqa: BLE001
                log.warning("getUpdates: %s", exc)
                time.sleep(5)
                continue

            settings = self.store.get_settings()
            interval = settings.get("scan_interval_min", 15.0) * 60
            owner = self.store.get_owner_chat_id()
            if owner and not settings["paused"] and time.time() - self._last_scan_ts >= interval:
                self._run_scan_and_alert(owner, manual=False)

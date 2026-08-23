"""Сховище налаштувань і чиста логіка Telegram-бота (без мережі)."""
import os
import tempfile
import unittest

from spreadbot.config import Config
from spreadbot.notify.telegram_bot import TelegramBot
from spreadbot.storage.db import Storage
from spreadbot.storage.settings_schema import DEFAULT_BOT_SETTINGS, clamp


class TestSettingsStorage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
        self.tmp.close()
        self.store = Storage(self.tmp.name)

    def tearDown(self):
        self.store.close()
        os.unlink(self.tmp.name)

    def test_defaults_when_unset(self):
        self.assertEqual(self.store.get_settings(), DEFAULT_BOT_SETTINGS)

    def test_update_settings_persists(self):
        self.store.update_settings(capital_usd=7500.0, paused=True)
        s = self.store.get_settings()
        self.assertEqual(s["capital_usd"], 7500.0)
        self.assertTrue(s["paused"])
        # інші поля лишились дефолтними
        self.assertEqual(s["min_edge_pp"], DEFAULT_BOT_SETTINGS["min_edge_pp"])

    def test_owner_chat_id_roundtrip(self):
        self.assertIsNone(self.store.get_owner_chat_id())
        self.store.set_owner_chat_id("12345")
        self.assertEqual(self.store.get_owner_chat_id(), "12345")

    def test_alert_dedup_respects_cooldown(self):
        self.assertFalse(self.store.was_alerted_recently("k1", "chat1", 60))
        self.store.mark_alerted("k1", "chat1")
        self.assertTrue(self.store.was_alerted_recently("k1", "chat1", 60))
        # інший чат / інший ключ — не під кулдауном
        self.assertFalse(self.store.was_alerted_recently("k1", "chat2", 60))
        self.assertFalse(self.store.was_alerted_recently("k2", "chat1", 60))
        # кулдаун 0 хвилин — уже "застарів"
        self.assertFalse(self.store.was_alerted_recently("k1", "chat1", 0))


class TestClamp(unittest.TestCase):
    def test_clamp_stays_in_bounds(self):
        self.assertEqual(clamp("capital_usd", -500.0), 100.0)
        self.assertEqual(clamp("capital_usd", 5_000_000.0), 1_000_000.0)
        self.assertEqual(clamp("min_edge_pp", 3.0), 3.0)


class TestBotMenu(unittest.TestCase):
    """Побудова меню й обробка callback-даних — чиста логіка, без мережі."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
        self.tmp.close()
        self.store = Storage(self.tmp.name)
        self.bot = TelegramBot("dummy-token", self.store, Config())

    def tearDown(self):
        self.store.close()
        os.unlink(self.tmp.name)

    def test_menu_text_reflects_settings(self):
        self.store.update_settings(capital_usd=12000.0, paused=True)
        text = self.bot._menu_text(self.store.get_settings())
        self.assertIn("$12,000", text)
        self.assertIn("на паузі", text)

    def test_menu_keyboard_marks_active_venues(self):
        kb = self.bot._menu_keyboard(self.store.get_settings())
        flat = [btn["text"] for row in kb for btn in row]
        self.assertTrue(any("✅ bybit" in t for t in flat))
        self.assertTrue(any("⬜ deribit" in t for t in flat))

    def test_toggle_callback_updates_settings(self):
        self.store.set_owner_chat_id("42")
        cq = {
            "id": "cb1",
            "message": {"chat": {"id": 42}, "message_id": 1},
            "data": "toggle:opt:deribit",
        }
        # editMessageText/answerCallbackQuery підуть у мережу — підмінюємо на no-op
        self.bot._answer_callback = lambda *a, **k: None
        self.bot._edit_menu = lambda *a, **k: None
        self.bot._handle_callback(cq)
        self.assertIn("deribit", self.store.get_settings()["option_venues"])

    def test_adjust_callback_clamps(self):
        self.store.set_owner_chat_id("42")
        self.store.update_settings(min_edge_pp=0.0)
        cq = {
            "id": "cb2",
            "message": {"chat": {"id": 42}, "message_id": 1},
            "data": "adj:min_edge_pp:-0.5",
        }
        self.bot._answer_callback = lambda *a, **k: None
        self.bot._edit_menu = lambda *a, **k: None
        self.bot._handle_callback(cq)
        self.assertEqual(self.store.get_settings()["min_edge_pp"], 0.0)  # не пішло в мінус

    def test_non_owner_callback_ignored(self):
        self.store.set_owner_chat_id("42")
        cq = {
            "id": "cb3",
            "message": {"chat": {"id": 999}, "message_id": 1},
            "data": "pause:1",
        }
        self.bot._answer_callback = lambda *a, **k: None
        self.bot._handle_callback(cq)
        self.assertFalse(self.store.get_settings()["paused"])


if __name__ == "__main__":
    unittest.main()

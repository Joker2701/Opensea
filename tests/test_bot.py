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


class TestPositionTracking(unittest.TestCase):
    """Кнопка «взяв позицію» -> запис у БД -> нагадування при наближенні
    до порогу. Опортьюніті беремо з офлайн-сканера (реальний об'єкт, не мок)."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
        self.tmp.close()
        self.store = Storage(self.tmp.name)
        self.store.set_owner_chat_id("42")
        self.bot = TelegramBot("dummy-token", self.store, Config())
        self.bot._answer_callback = lambda *a, **k: None

        from spreadbot.adapters.offline import FIXTURE_DIR, offline_deribit, offline_polymarket
        from spreadbot.config import Config as _Config
        from spreadbot.scanner.scanner import Scanner

        cfg = _Config()
        cfg.offline = True
        cfg.scan.assets = ["ETH"]
        cfg.scan.min_days = 1
        pm = offline_polymarket(os.path.join(FIXTURE_DIR, "polymarket_crypto.json"))
        dr = offline_deribit(os.path.join(FIXTURE_DIR, "deribit_eth.json"))
        ops, self.surfaces = Scanner([pm], [dr], cfg).run_with_surfaces()
        self.assertTrue(ops, "фікстура має видавати хоч одну можливість")
        self.op = ops[0]

    def tearDown(self):
        self.store.close()
        os.unlink(self.tmp.name)

    def test_take_button_opens_position(self):
        h = self.bot._cache_op(self.op)
        cq = {"id": "cb", "message": {"chat": {"id": 42}, "message_id": 1}, "data": f"take:{h}"}
        sent = []
        self.bot.send = lambda *a, **k: sent.append(a)
        self.bot._handle_callback(cq)

        rows = self.store.list_open_positions("42")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["asset"], self.op.structure.prediction.market.claim.asset)
        self.assertTrue(any("Збережено як позиція" in s[1] for s in sent))

    def test_take_with_unknown_hash_is_graceful(self):
        cq = {"id": "cb", "message": {"chat": {"id": 42}, "message_id": 1}, "data": "take:doesnotexist"}
        sent = []
        self.bot.send = lambda *a, **k: sent.append(a)
        self.bot._handle_callback(cq)
        self.assertEqual(self.store.list_open_positions("42"), [])
        self.assertTrue(any("застаріла" in s[1] for s in sent))

    def test_close_button_closes_position(self):
        pos_id = self.bot._open_position_from_op("42", self.op)
        cq = {"id": "cb", "message": {"chat": {"id": 42}, "message_id": 1},
              "data": f"close_pos:{pos_id}"}
        self.bot.send = lambda *a, **k: None
        self.bot._handle_callback(cq)
        self.assertEqual(self.store.list_open_positions("42"), [])

    def test_proximity_check_warns_when_close_to_threshold(self):
        pos_id = self.bot._open_position_from_op("42", self.op)
        claim = self.op.structure.prediction.market.claim
        # підміняємо спот на сам поріг -> це має вважатись "торкнулось"
        fake_surfaces = {}
        for (asset, venue), (chain, surface) in self.surfaces.items():
            chain.spot = claim.threshold if asset == claim.asset else chain.spot
            fake_surfaces[(asset, venue)] = (chain, surface)

        sent = []
        self.bot.send = lambda *a, **k: sent.append(a)
        self.bot._check_position_proximity("42", fake_surfaces)
        self.assertTrue(any("Час продавати опціон" in s[1] for s in sent))

        # повторний виклик одразу — під кулдауном, дублю не шлемо
        sent.clear()
        self.bot._check_position_proximity("42", fake_surfaces)
        self.assertEqual(sent, [])

    def test_positions_command_lists_open(self):
        self.bot._open_position_from_op("42", self.op)
        sent = []
        self.bot.send = lambda *a, **k: sent.append(a)
        self.bot._cmd_positions("42")
        self.assertEqual(len(sent), 1)
        self.assertIn(self.op.structure.prediction.market.claim.asset, sent[0][1])


if __name__ == "__main__":
    unittest.main()

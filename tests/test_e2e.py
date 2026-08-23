"""Наскрізний прогін сканера на фікстурах — без мережі."""
import math
import os
import unittest

from spreadbot.adapters.offline import FIXTURE_DIR, offline_deribit, offline_polymarket
from spreadbot.config import Config
from spreadbot.report import mtm_heatmap, opportunity_card, scenario_table, stress_test
from spreadbot.scanner.scanner import Scanner


class TestEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cfg = Config()
        cfg.offline = True
        cfg.scan.assets = ["ETH"]
        cfg.scan.min_days = 1
        cls.cfg = cfg
        pm = offline_polymarket(os.path.join(FIXTURE_DIR, "polymarket_crypto.json"))
        dr = offline_deribit(os.path.join(FIXTURE_DIR, "deribit_eth.json"))
        cls.scanner = Scanner([pm], [dr], cfg)
        cls.surfaces = cls.scanner.load_surfaces()
        cls.views = cls.scanner.screen(cls.surfaces)
        cls.ops = cls.scanner.build(cls.views, cls.surfaces)

    def test_markets_are_screened(self):
        self.assertTrue(self.views)
        for v in self.views:
            self.assertGreaterEqual(abs(v.edge_pp), self.cfg.scan.min_edge_pp)

    def test_touch_market_uses_barrier_model(self):
        touch = [v for v in self.views if v.market.claim.is_touch]
        self.assertTrue(touch)
        self.assertIn("one-touch", touch[0].model.method)

    def test_opportunities_are_produced_and_sane(self):
        self.assertTrue(self.ops)
        for op in self.ops:
            m = op.metrics
            self.assertGreater(m.capital, 0)
            self.assertTrue(all(math.isfinite(x) for x in (m.ev_pnl, m.worst_pnl, m.cvar_pnl)))
            self.assertLessEqual(m.worst_pnl, m.ev_pnl)
            self.assertLessEqual(m.ev_pnl, m.best_pnl)
            self.assertGreaterEqual(op.score, 0.0)

    def test_ranked_descending(self):
        scores = [o.score for o in self.ops]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_reports_render(self):
        op = self.ops[0]
        surface = next(iter(self.surfaces.values()))[1]
        for text in (
            opportunity_card(op),
            scenario_table(op),
            mtm_heatmap(op, surface),
            stress_test(op, surface),
        ):
            self.assertIsInstance(text, str)
            self.assertGreater(len(text), 50)


if __name__ == "__main__":
    unittest.main()

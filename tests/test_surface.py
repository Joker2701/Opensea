"""Поверхня волатильності: інтерполяція і відновлення IV з ланцюга."""
import datetime as dt
import math
import os
import unittest

from spreadbot.adapters.offline import FIXTURE_DIR, offline_deribit
from spreadbot.pricing.surface import VolSurface


class TestSurface(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.chain = offline_deribit(os.path.join(FIXTURE_DIR, "deribit_eth.json")).fetch_chain("ETH")
        cls.surface = VolSurface.from_chain(cls.chain)

    def test_slices_match_expiries(self):
        self.assertEqual(len(self.surface.slices), len(self.chain.expiries()))

    def test_iv_recovers_quoted_value(self):
        expiry = self.chain.expiries()[2]
        q = self.chain.nearest_strike(expiry, self.chain.spot, __import__(
            "spreadbot.models", fromlist=["OptionType"]).OptionType.CALL)
        t = (expiry - self.chain.fetched_at).total_seconds() / (365 * 86400)
        self.assertAlmostEqual(self.surface.iv(q.strike, t), q.iv_mark, delta=0.01)

    def test_total_variance_is_non_decreasing(self):
        """Календарний арбітраж: w = sigma^2 * T має зростати за терміном."""
        strike = self.chain.spot
        prev = 0.0
        for t in (0.1, 0.25, 0.5, 0.75, 1.0):
            w = self.surface.iv(strike, t) ** 2 * t
            self.assertGreaterEqual(w + 1e-9, prev)
            prev = w

    def test_flat_extrapolation_beyond_strikes(self):
        t = 0.5
        far = self.surface.iv(self.chain.spot * 50, t)
        edge = self.surface.iv(self.chain.spot * 10, t)   # обидва за межами страйків
        self.assertAlmostEqual(far, edge, places=6)

    def test_skew_is_negative_for_put_skewed_smile(self):
        self.assertLess(self.surface.skew(self.chain.spot, 0.5), 0.0)

    def test_forward_above_spot_in_contango(self):
        self.assertGreater(self.surface.forward(0.85), self.surface.spot)


if __name__ == "__main__":
    unittest.main()

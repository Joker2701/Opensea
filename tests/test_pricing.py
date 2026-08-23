"""Математика ціноутворення: тотожності, які мають виконуватись точно."""
import math
import unittest

from spreadbot.pricing.bs import black76, implied_vol, vega
from spreadbot.pricing.digital import (
    digital_call,
    digital_from_call_spread,
    first_passage_density,
    prob_touch,
    prob_touch_and_finish_below,
    touch_time_buckets,
)

F, K, T, SIG = 2450.0, 2600.0, 0.85, 0.60


class TestBlack76(unittest.TestCase):
    def test_put_call_parity(self):
        c = black76(F, K, T, SIG, True)
        p = black76(F, K, T, SIG, False)
        self.assertAlmostEqual(c - p, F - K, places=8)

    def test_implied_vol_roundtrip(self):
        for sig in (0.2, 0.6, 1.5):
            px = black76(F, K, T, sig, True)
            self.assertAlmostEqual(implied_vol(px, F, K, T, True), sig, places=6)

    def test_price_is_monotone_in_vol(self):
        prices = [black76(F, K, T, s, True) for s in (0.3, 0.6, 0.9)]
        self.assertLess(prices[0], prices[1])
        self.assertLess(prices[1], prices[2])

    def test_vega_matches_finite_difference(self):
        h = 1e-5
        fd = (black76(F, K, T, SIG + h, True) - black76(F, K, T, SIG - h, True)) / (2 * h)
        self.assertAlmostEqual(vega(F, K, T, SIG), fd, delta=1e-3)


class TestDigital(unittest.TestCase):
    def test_digital_equals_negative_dc_dk(self):
        h = 0.01
        fd = -(black76(F, K + h, T, SIG, True) - black76(F, K - h, T, SIG, True)) / (2 * h)
        self.assertAlmostEqual(digital_call(F, K, T, SIG), fd, delta=1e-4)

    def test_digital_with_skew_is_lower_when_smile_falls(self):
        # спадна усмішка (скью < 0) робить цифровий колл ДОРОЖЧИМ
        flat = digital_call(F, K, T, SIG, 1.0, 0.0)
        skewed = digital_call(F, K, T, SIG, 1.0, -1e-4)
        self.assertGreater(skewed, flat)

    def test_call_spread_replication(self):
        c_lo = black76(F, 2500, T, SIG, True)
        c_hi = black76(F, 2600, T, SIG, True)
        approx = digital_from_call_spread(c_lo, c_hi, 2500, 2600)
        exact = digital_call(F, 2550, T, SIG)
        self.assertAlmostEqual(approx, exact, delta=0.01)


class TestBarrier(unittest.TestCase):
    S, B = 2400.0, 4000.0

    def test_touch_decomposition(self):
        """P(touch) = P(touch & S_T<=B) + P(S_T>B) — точна тотожність."""
        lhs = prob_touch(self.S, self.B, T, SIG)
        rhs = prob_touch_and_finish_below(self.S, self.B, self.B, T, SIG) + digital_call(
            self.S, self.B, T, SIG
        )
        self.assertAlmostEqual(lhs, rhs, places=9)

    def test_touch_roughly_double_terminal(self):
        """Класичне правило: торкнутись ~вдвічі імовірніше, ніж закритись вище."""
        touch = prob_touch(self.S, self.B, T, SIG)
        terminal = digital_call(self.S, self.B, T, SIG)
        self.assertGreater(touch, 1.7 * terminal)
        self.assertLess(touch, 2.6 * terminal)

    def test_touch_monotone_in_time_and_vol(self):
        self.assertLess(
            prob_touch(self.S, self.B, 0.25, SIG), prob_touch(self.S, self.B, 1.0, SIG)
        )
        self.assertLess(
            prob_touch(self.S, self.B, T, 0.4), prob_touch(self.S, self.B, T, 0.9)
        )

    def test_first_passage_mass_equals_touch_probability(self):
        buckets = touch_time_buckets(self.S, self.B, T, SIG, 0.0, 32)
        self.assertAlmostEqual(
            sum(m for _, m in buckets), prob_touch(self.S, self.B, T, SIG), places=6
        )

    def test_first_passage_density_positive(self):
        self.assertGreater(first_passage_density(self.S, self.B, 0.3, SIG), 0.0)

    def test_touch_below_symmetry(self):
        """Бар'єр знизу при нульовому зносі — дзеркало бар'єра зверху."""
        up = prob_touch(100.0, 200.0, T, SIG, mu=0.5 * SIG * SIG)
        down = prob_touch(100.0, 50.0, T, SIG, mu=0.5 * SIG * SIG)
        self.assertAlmostEqual(up, down, places=6)


if __name__ == "__main__":
    unittest.main()

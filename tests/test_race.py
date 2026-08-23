"""Монте-Карло гонки до одного з двох бар'єрів: перевірка на відомих
граничних випадках, бо замкненої формули для скінченного часу немає."""
import math
import unittest

from spreadbot.pricing.race import (
    ruin_probability_infinite_horizon,
    simulate_race,
)


class TestRuinFormula(unittest.TestCase):
    """Аналітична формула (T->inf) — точка звірки для Монте-Карло."""

    def test_boundary_values(self):
        # старт впритул до нижнього бар'єра -> 0; впритул до верхнього -> 1
        self.assertAlmostEqual(
            ruin_probability_infinite_horizon(100.0, 99.999, 200.0, 0.6), 0.0, places=2
        )

    def test_no_drift_is_linear_in_log_space(self):
        # без зносу (nu=0, mu=0) -> частка ЛОГ-відстані до верхнього бар'єра
        p = ruin_probability_infinite_horizon(100.0, 50.0, 200.0, sigma=0.5, mu=0.5 * 0.5 ** 2)
        a, b = math.log(0.5), math.log(2.0)
        self.assertAlmostEqual(p, -a / (b - a), places=6)

    def test_positive_drift_favors_upper(self):
        p_flat = ruin_probability_infinite_horizon(100.0, 50.0, 200.0, sigma=0.5, mu=0.0)
        p_up = ruin_probability_infinite_horizon(100.0, 50.0, 200.0, sigma=0.5, mu=0.3)
        self.assertGreater(p_up, p_flat)


class TestSimulateRace(unittest.TestCase):
    def test_probabilities_sum_to_one(self):
        res = simulate_race(2400, 1200, 4800, t=0.5, sigma=0.6, n_paths=2000, n_steps=100)
        self.assertAlmostEqual(res.p_upper + res.p_lower + res.p_neither, 1.0, places=9)

    def test_converges_to_infinite_horizon_ruin_formula(self):
        # дуже довгий горизонт -> "жодного" майже не лишається, MC має
        # збігтись до замкненої формули
        spot, lower, upper, sigma = 2400.0, 1000.0, 6000.0, 0.6
        res = simulate_race(spot, lower, upper, t=50.0, sigma=sigma, mu=0.0,
                             n_paths=15_000, n_steps=400, keep_outcomes=False)
        theory = ruin_probability_infinite_horizon(spot, lower, upper, sigma, 0.0)
        self.assertLess(res.p_neither, 0.01)
        self.assertAlmostEqual(res.p_upper, theory, delta=0.02)

    def test_symmetric_barriers_zero_log_drift_give_equal_odds(self):
        # симетричні в ЛОГ-просторі бар'єри + нульовий знос ЛОГ-ціни
        # (це НЕ mu=0 у прайс-просторі — там 0.5σ² поправка Іто) ->
        # однакові шанси на верхній і нижній
        spot, sigma = 2400.0, 0.6
        upper = spot * math.exp(0.8)
        lower = spot * math.exp(-0.8)
        mu_zero_log_drift = 0.5 * sigma * sigma
        res = simulate_race(spot, lower, upper, t=1.0, sigma=sigma, mu=mu_zero_log_drift,
                             n_paths=20_000, n_steps=200, keep_outcomes=False)
        self.assertAlmostEqual(res.p_upper, res.p_lower, delta=0.02)

    def test_closer_barrier_wins_more_often(self):
        spot, sigma = 2400.0, 0.6
        # верхній ближче за нижній -> більше шансів на верхній
        res = simulate_race(spot, spot * 0.3, spot * 1.3, t=1.0, sigma=sigma,
                             n_paths=6000, n_steps=150, keep_outcomes=False)
        self.assertGreater(res.p_upper, res.p_lower)

    def test_is_deterministic_for_fixed_seed(self):
        kwargs = dict(spot=2400, lower=1200, upper=4800, t=0.5, sigma=0.6,
                      n_paths=500, n_steps=50, keep_outcomes=False)
        r1 = simulate_race(seed=42, **kwargs)
        r2 = simulate_race(seed=42, **kwargs)
        self.assertEqual(r1.p_upper, r2.p_upper)
        self.assertEqual(r1.p_lower, r2.p_lower)

    def test_outcomes_carry_hit_time_and_level(self):
        res = simulate_race(2400, 1200, 4800, t=0.5, sigma=0.6, n_paths=300, n_steps=80)
        touched = [o for o in res.outcomes if o.winner != "neither"]
        self.assertTrue(touched)
        for o in touched:
            self.assertIsNotNone(o.tau_years)
            self.assertGreater(o.tau_years, 0.0)
            self.assertLessEqual(o.tau_years, 0.5 + 1e-9)
            expected = 4800 if o.winner == "upper" else 1200
            self.assertAlmostEqual(o.s_final, expected, delta=1.0)

    def test_rejects_spot_outside_barriers(self):
        with self.assertRaises(ValueError):
            simulate_race(100, 200, 300, t=1.0, sigma=0.5, n_paths=10, n_steps=5)


if __name__ == "__main__":
    unittest.main()

"""Сайзинг, облік капіталу і сценарний рушій."""
import datetime as dt
import os
import unittest

from spreadbot.adapters.offline import FIXTURE_DIR, offline_deribit, offline_polymarket
from spreadbot.models import Side
from spreadbot.pricing.surface import VolSurface
from spreadbot.strategy.constructors import build_candidates
from spreadbot.strategy.costs import (
    DEFAULT_OPTION_FEES,
    DEFAULT_PREDICTION_FEES,
    OptionFees,
    PredictionFees,
    walk_book,
)
from spreadbot.strategy.payoff import build_scenarios, evaluate, make_pnl_fn
from spreadbot.strategy.sizing import SizingConfig, build_structure, solve
from spreadbot.models import Level, OrderBook


def _setup():
    now = dt.datetime.now(dt.timezone.utc)
    chain = offline_deribit(os.path.join(FIXTURE_DIR, "deribit_eth.json")).fetch_chain("ETH")
    surface = VolSurface.from_chain(chain)
    pm = offline_polymarket(os.path.join(FIXTURE_DIR, "polymarket_crypto.json"))
    market = pm.list_markets(assets=["ETH"], min_days=1, max_days=2000)[0]
    pm.load_books(market)
    return now, chain, surface, market


class TestBookExecution(unittest.TestCase):
    def test_vwap_walks_levels(self):
        book = OrderBook(asks=[Level(0.62, 100), Level(0.63, 100)])
        fill = walk_book(book, 150, Side.BUY)
        self.assertTrue(fill.filled)
        self.assertAlmostEqual(fill.avg_price, (100 * 0.62 + 50 * 0.63) / 150, places=9)

    def test_limit_price_stops_the_walk(self):
        book = OrderBook(asks=[Level(0.62, 100), Level(0.70, 100)])
        fill = walk_book(book, 150, Side.BUY, limit=0.65)
        self.assertFalse(fill.filled)
        self.assertEqual(fill.size, 100)


class TestFees(unittest.TestCase):
    def test_option_fee_is_capped_by_premium(self):
        fees = OptionFees(taker_pct_of_underlying=0.0003, cap_pct_of_premium=0.125)
        # дешевий OTM: 0.03% від споту > 12.5% премії -> діє стеля
        self.assertAlmostEqual(fees.trade_fee(1.0, 1.0, 3000.0), 0.125, places=9)
        # дорогий ITM: діє відсоток від споту
        self.assertAlmostEqual(fees.trade_fee(1.0, 500.0, 3000.0), 0.9, places=9)

    def test_kalshi_formula(self):
        fees = PredictionFees(parabolic_taker_fee=True)
        self.assertGreater(fees.entry_fee(100, 0.5), fees.entry_fee(100, 0.05))

    def test_polymarket_market_order_fee_matches_source_example(self):
        # $500 на 1000 шейрів по 50% -> $18 комісії (1,8% від шейрів,
        # 3,6% від вкладених грошей) — приклад із гайду користувача
        fees = DEFAULT_PREDICTION_FEES["polymarket"]
        fee = fees.entry_fee(size=1000, price=0.5)
        self.assertAlmostEqual(fee, 18.0, places=1)
        self.assertAlmostEqual(fee / 500.0, 0.036, places=2)


class TestScenarios(unittest.TestCase):
    def test_probabilities_sum_to_one(self):
        now, chain, surface, market = _setup()
        for policy in ("hold", "unwind"):
            scs = build_scenarios(market.claim, surface, now, n=40, policy=policy)
            self.assertAlmostEqual(sum(s.prob for s in scs), 1.0, places=6)

    def test_unwind_policy_creates_touch_time_scenarios(self):
        now, chain, surface, market = _setup()
        scs = build_scenarios(market.claim, surface, now, n=40, policy="unwind")
        self.assertTrue(any(s.unwind and s.tau_years for s in scs))


class TestSizing(unittest.TestCase):
    def test_lot_rounding_preserves_leg_ratio(self):
        now, chain, surface, market = _setup()
        cand = build_candidates(market, chain, strike_ratios=[0.65])[0]
        cfg = SizingConfig(capital_usd=10_000)
        st = build_structure(
            cand, 0.7, 25_000, chain.spot, cfg,
            DEFAULT_PREDICTION_FEES["polymarket"], DEFAULT_OPTION_FEES["deribit"],
            round_lots=True,
        )
        self.assertIsNotNone(st)
        qty = st.options[0].qty
        self.assertAlmostEqual(qty % st.options[0].quote.min_qty, 0.0, places=9)
        self.assertAlmostEqual(qty / st.prediction.size * 1000, 0.7, places=6)

    def test_capital_nets_short_leg_credit(self):
        # Спред тепер генерується лише для at-expiry ринків (для touch —
        # небезпечний через ранній вихід і вимкнений повністю, див.
        # constructors.py). Беремо ринок "above_at_expiry" з фікстури.
        now, chain, surface, _ = _setup()
        pm = offline_polymarket(os.path.join(FIXTURE_DIR, "polymarket_crypto.json"))
        market = next(
            m for m in pm.list_markets(assets=["ETH"], min_days=1, max_days=2000)
            if m.claim.kind.value == "above_at_expiry"
        )
        pm.load_books(market)
        spreads = [c for c in build_candidates(market, chain) if len(c.options) == 2]
        self.assertTrue(spreads)
        cfg = SizingConfig()
        st = build_structure(
            spreads[0], 1.0, 10_000, chain.spot, cfg,
            DEFAULT_PREDICTION_FEES["polymarket"], DEFAULT_OPTION_FEES["deribit"],
            round_lots=False,
        )
        gross = st.prediction.cost + sum(max(l.cost, 0) for l in st.options)
        self.assertLess(st.capital, gross)   # кредит від проданої ноги врахований

    def test_unwind_policy_beats_hold_on_tail(self):
        """Головна теза бота: хедж працює лише якщо опціон продають на бар'єрі."""
        now, chain, surface, market = _setup()
        cand = [c for c in build_candidates(market, chain, strike_ratios=[0.65])
                if len(c.options) == 1][0]
        cfg = SizingConfig()
        st = build_structure(
            cand, 0.8, 10_000, chain.spot, cfg,
            DEFAULT_PREDICTION_FEES["polymarket"], DEFAULT_OPTION_FEES["deribit"],
            round_lots=False,
        )
        days = market.claim.days_to_deadline(now)
        hold = evaluate(st, build_scenarios(market.claim, surface, now, n=60), days)
        unw_scs = build_scenarios(market.claim, surface, now, n=60, policy="unwind")
        unwind = evaluate(
            st, unw_scs, days, pnl_fn=make_pnl_fn(market.claim, surface, now)
        )
        self.assertLess(hold.worst_return, -0.9)
        self.assertGreater(unwind.worst_return, hold.worst_return + 0.5)

    def test_solver_respects_risk_limit(self):
        now, chain, surface, market = _setup()
        scs = build_scenarios(market.claim, surface, now, n=60, policy="unwind")
        fn = make_pnl_fn(market.claim, surface, now)
        cfg = SizingConfig(capital_usd=10_000, max_loss_frac=0.15)
        days = market.claim.days_to_deadline(now)
        found = 0
        for cand in build_candidates(market, chain):
            res = solve(
                cand, scs, chain.spot, days, cfg,
                DEFAULT_PREDICTION_FEES["polymarket"], DEFAULT_OPTION_FEES["deribit"],
                market_prob=0.37, model_prob=0.2748, pnl_fn=fn,
            )
            if res is None:
                continue
            found += 1
            self.assertGreaterEqual(res.metrics.cvar_return, -0.15 - 1e-9)
            self.assertGreaterEqual(res.metrics.bet_win_floor, -1e-6)
            self.assertLessEqual(res.structure.capital, cfg.capital_usd * 1.05)
        self.assertGreater(found, 0)


if __name__ == "__main__":
    unittest.main()


class TestForkConditions(unittest.TestCase):
    """Дві умови стратегії — головний критерій бота."""

    def test_conditions_hold_for_emitted_structures(self):
        from spreadbot.strategy.fork import check as fork_check

        now, chain, surface, market = _setup()
        scs = build_scenarios(market.claim, surface, now, n=60, policy="unwind")
        fn = make_pnl_fn(market.claim, surface, now)
        cfg = SizingConfig(capital_usd=10_000, require_fork=True)
        days = market.claim.days_to_deadline(now)
        emitted = 0
        for cand in build_candidates(market, chain):
            res = solve(
                cand, scs, chain.spot, days, cfg,
                DEFAULT_PREDICTION_FEES["polymarket"], DEFAULT_OPTION_FEES["deribit"],
                market_prob=0.37, model_prob=0.2748, pnl_fn=fn,
            )
            if res is None:
                continue
            emitted += 1
            fc = fork_check(res.structure, market.claim)
            self.assertTrue(fc.ok_a, "умова A має виконуватись")
            self.assertTrue(fc.ok_b, "умова B має виконуватись")
            self.assertTrue(res.metrics.fork_ok)
        self.assertGreater(emitted, 0)

    def test_ratio_window_matches_analytic_bounds(self):
        """Аналітичне вікно r ∈ [1000p/(B-K), 1000(1-p)/c] — орієнтир для солвера."""
        from spreadbot.strategy.fork import (
            max_ratio_for_condition_a,
            min_ratio_for_condition_b,
        )

        lo = min_ratio_for_condition_b(bet_price=0.63, barrier=4000, strike=2600, premium_per_unit=500)
        hi = max_ratio_for_condition_a(bet_price=0.63, premium_per_unit=500)
        self.assertLess(lo, hi)                 # вікно непорожнє -> вилка існує
        self.assertAlmostEqual(hi, 1000 * 0.37 / 500, places=9)
        self.assertAlmostEqual(lo, 1000 * 0.63 / (1400 - 500), places=9)

    def test_condition_b_fails_when_strike_above_barrier(self):
        """Страйк вище порогу -> на порозі опціон без внутрішньої вартості."""
        from spreadbot.strategy.fork import min_ratio_for_condition_b

        self.assertEqual(
            min_ratio_for_condition_b(0.63, barrier=4000, strike=4500, premium_per_unit=100),
            float("inf"),
        )


class TestDownsideBoundedByFriction(unittest.TestCase):
    """Головна властивість, яку очікує користувач: НІЯКОГО мінусу взагалі —
    найгірший сценарій по всьому розподілу не може бути гіршим за нуль.
    Це жорсткий гейт солвера (`min_worst_return=0.0` за замовчуванням), а
    не побажання: кандидат, для якого немає жодного співвідношення ніг і
    жодного лоту з worst_return >= 0, просто не потрапляє у видачу.

    Спреди (довгий+короткий опціон) свідомо виключені з цього тесту й
    вимкнені за замовчуванням: коротка нога має власну часову вартість, і
    її відкуп при ранньому торканні може коштувати більше, ніж дає інтринсик
    на порозі — тобто для спреду ця гарантія НЕ тримається.
    """

    def test_worst_case_never_negative_for_single_leg(self):
        now, chain, surface, market = _setup()
        cfg = SizingConfig(capital_usd=10_000, unwind_cost_frac=0.01)
        pf, of = DEFAULT_PREDICTION_FEES["polymarket"], DEFAULT_OPTION_FEES["deribit"]
        scs = build_scenarios(market.claim, surface, now, n=60, policy="unwind")
        fn = make_pnl_fn(market.claim, surface, now, cfg.unwind_cost_frac)
        days = market.claim.days_to_deadline(now)
        found = 0
        for cand in build_candidates(market, chain, include_spreads=False):
            res = solve(
                cand, scs, chain.spot, days, cfg, pf, of,
                market_prob=market.implied_yes or 0.0, model_prob=0.0, pnl_fn=fn,
            )
            if res is None:
                continue
            found += 1
            self.assertGreaterEqual(res.metrics.worst_return, -1e-6)
            self.assertEqual(res.metrics.prob_loss, 0.0)
        self.assertGreater(found, 0)

    def test_narrow_window_found_at_realistic_capital(self):
        """Вікно, де worst>=0, буває вузьким і залежить від масштабу
        (проковзування по стакану) — солвер має знаходити його, а не
        втрачати через грубу сітку чи невдале округлення лоту."""
        now, chain, surface, market = _setup()
        pf, of = DEFAULT_PREDICTION_FEES["polymarket"], DEFAULT_OPTION_FEES["deribit"]
        scs = build_scenarios(market.claim, surface, now, n=60, policy="unwind")
        fn = make_pnl_fn(market.claim, surface, now, 0.01)
        days = market.claim.days_to_deadline(now)
        cand = next(
            c for c in build_candidates(market, chain, include_spreads=False)
            if "2800" in c.name
        )
        for capital in (2_000.0, 8_000.0, 10_000.0):
            cfg = SizingConfig(capital_usd=capital, unwind_cost_frac=0.01)
            res = solve(
                cand, scs, chain.spot, days, cfg, pf, of,
                market_prob=market.implied_yes or 0.0, model_prob=0.0, pnl_fn=fn,
            )
            self.assertIsNotNone(res, f"capital={capital}: вікно не знайдено")
            self.assertGreaterEqual(res.metrics.worst_return, -1e-6)

    def test_oversized_position_is_refused_not_faked(self):
        """Глибина стакана скінченна: якщо запитаний капітал завеликий для
        книги (ковзання з'їдає весь запас умов A/B), солвер має ВІДМОВИТИ,
        а не видати конструкцію, для якої гарантія насправді не тримається."""
        now, chain, surface, market = _setup()
        pf, of = DEFAULT_PREDICTION_FEES["polymarket"], DEFAULT_OPTION_FEES["deribit"]
        scs = build_scenarios(market.claim, surface, now, n=60, policy="unwind")
        fn = make_pnl_fn(market.claim, surface, now, 0.01)
        days = market.claim.days_to_deadline(now)
        cand = next(
            c for c in build_candidates(market, chain, include_spreads=False)
            if "2800" in c.name
        )
        cfg = SizingConfig(capital_usd=500_000.0, unwind_cost_frac=0.01)
        res = solve(
            cand, scs, chain.spot, days, cfg, pf, of,
            market_prob=market.implied_yes or 0.0, model_prob=0.0, pnl_fn=fn,
        )
        # інваріант тримається за будь-якого результату: або чесна відмова,
        # або (якщо все ж знайшлось) worst_return все одно >= 0
        if res is not None:
            self.assertGreaterEqual(res.metrics.worst_return, -1e-6)


class TestAtExpirySafety(unittest.TestCase):
    """At-expiry ринки (не тільки touch) теж можуть проходити гарантію
    worst>=0 — опціон і ставка резолвляться в один день, тому спред тут
    безпечний (на відміну від touch, де рання розхеджа коштує дорожче за
    інтринсик). Фікстурний at-expiry ринок сам по собі не має достатнього
    едж для жодної конструкції (перевірено вручну) — тому тут підставляємо
    трохи дешевшу ставку, щоб довести, що МЕХАНІЗМ працює, коли цифри
    сходяться, а не тільки для touch-ринків.
    """

    def test_favorable_at_expiry_market_finds_zero_loss_structure(self):
        now, chain, surface, _ = _setup()
        pm = offline_polymarket(os.path.join(FIXTURE_DIR, "polymarket_crypto.json"))
        market = next(
            m for m in pm.list_markets(assets=["ETH"], min_days=1, max_days=2000)
            if m.claim.kind.value == "above_at_expiry"
        )
        pm.load_books(market)

        def book(price, depth=3000, n=5, tick=0.005):
            return OrderBook(
                bids=[Level(round(price - tick * (i + 1), 4), depth * (i + 1)) for i in range(n)],
                asks=[Level(round(price + tick * i, 4), depth * (i + 1)) for i in range(n)],
            )

        market.no_book = book(0.32)   # штучно дешевша ставка NO (з запасом понад комісію маркет-ноги)
        market.yes_book = book(0.68)

        scs = build_scenarios(market.claim, surface, now, n=60, policy="unwind")
        fn = make_pnl_fn(market.claim, surface, now, 0.01)
        cfg = SizingConfig(capital_usd=10_000, unwind_cost_frac=0.01)
        pf, of = DEFAULT_PREDICTION_FEES["polymarket"], DEFAULT_OPTION_FEES["deribit"]
        days = market.claim.days_to_deadline(now)

        found = 0
        for cand in build_candidates(market, chain):
            res = solve(
                cand, scs, chain.spot, days, cfg, pf, of,
                market_prob=0.68, model_prob=0.0, pnl_fn=fn,
            )
            if res is None:
                continue
            found += 1
            self.assertGreaterEqual(res.metrics.worst_return, -1e-6)
        self.assertGreater(found, 0, "механізм at-expiry має знаходити вилку при сприятливих цифрах")

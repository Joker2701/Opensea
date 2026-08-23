"""Розбір заголовків ринків. Головне правило: краще None, ніж здогадка."""
import datetime as dt
import unittest

from spreadbot.models import ClaimKind
from spreadbot.parsing.claim import parse_title


class TestParseTitle(unittest.TestCase):
    def test_touch_above(self):
        c = parse_title("Will Ethereum hit $4,000 by June 30, 2027?")
        self.assertIsNotNone(c)
        self.assertEqual(c.asset, "ETH")
        self.assertEqual(c.kind, ClaimKind.TOUCH_ABOVE)
        self.assertEqual(c.threshold, 4000.0)
        self.assertEqual(c.deadline.date(), dt.date(2027, 6, 30))

    def test_k_suffix(self):
        c = parse_title("Will ETH test 4k by Sep 30, 2027?")
        self.assertEqual(c.threshold, 4000.0)

    def test_date_number_is_not_a_price(self):
        c = parse_title("By June 30, 2027 will ETH hit 4000?")
        self.assertEqual(c.threshold, 4000.0)

    def test_at_expiry(self):
        c = parse_title("Will ETH be above $3,000 on December 31, 2026?")
        self.assertEqual(c.kind, ClaimKind.ABOVE_AT_EXPIRY)

    def test_downside_touch(self):
        c = parse_title("Will Bitcoin dip to $50k by March 31, 2027?")
        self.assertEqual(c.kind, ClaimKind.TOUCH_BELOW)
        self.assertEqual(c.threshold, 50000.0)

    def test_unparseable_returns_none(self):
        self.assertIsNone(parse_title("Will Trump win the election?"))
        self.assertIsNone(parse_title("Will ETH flip BTC?"))

    def test_end_date_from_api_wins(self):
        end = dt.datetime(2027, 6, 30, 23, 59, tzinfo=dt.timezone.utc)
        c = parse_title("Will ETH hit $4,000 this cycle?", end_date=end)
        self.assertEqual(c.deadline, end)


class TestRaceParsing(unittest.TestCase):
    END = dt.datetime(2027, 1, 1, tzinfo=dt.timezone.utc)

    def test_before_pattern(self):
        c = parse_title("Will BTC hit $150,000 before $70,000?", end_date=self.END)
        self.assertEqual(c.kind, ClaimKind.RACE_UPPER_FIRST)
        self.assertEqual(c.threshold, 150_000.0)
        self.assertEqual(c.race_other_threshold, 70_000.0)

    def test_before_pattern_lower_first(self):
        c = parse_title("Will BTC hit $70,000 before $150,000?", end_date=self.END)
        self.assertEqual(c.kind, ClaimKind.RACE_LOWER_FIRST)
        self.assertEqual(c.threshold, 70_000.0)
        self.assertEqual(c.race_other_threshold, 150_000.0)

    def test_or_first_pattern(self):
        c = parse_title("BTC: $150k or $70k first?", end_date=self.END)
        self.assertEqual(c.kind, ClaimKind.RACE_UPPER_FIRST)
        self.assertEqual(c.threshold, 150_000.0)

    def test_k_suffix_in_race(self):
        c = parse_title("Will BTC hit $100k before it hits $70k?", end_date=self.END)
        self.assertEqual(c.threshold, 100_000.0)
        self.assertEqual(c.race_other_threshold, 70_000.0)

    def test_race_does_not_shadow_ordinary_touch(self):
        c = parse_title("Will Ethereum hit $4,000 by June 30, 2027?", end_date=self.END)
        self.assertEqual(c.kind, ClaimKind.TOUCH_ABOVE)
        self.assertIsNone(c.race_other_threshold)

    def test_resolves_yes_not_implemented_for_race(self):
        c = parse_title("Will BTC hit $150,000 before $70,000?", end_date=self.END)
        with self.assertRaises(NotImplementedError):
            c.resolves_yes(path_max=200_000, path_min=50_000, s_final=160_000)


class TestClaimLogic(unittest.TestCase):
    def test_touch_resolution(self):
        c = parse_title("Will ETH hit $4,000 by June 30, 2027?")
        self.assertTrue(c.resolves_yes(path_max=4100, path_min=1000, s_final=1500))
        self.assertFalse(c.resolves_yes(path_max=3900, path_min=1000, s_final=3800))

    def test_at_expiry_resolution(self):
        c = parse_title("Will ETH be above $3,000 on December 31, 2026?")
        # шлях не має значення — тільки фінал
        self.assertFalse(c.resolves_yes(path_max=9000, path_min=100, s_final=2999))
        self.assertTrue(c.resolves_yes(path_max=3001, path_min=3000, s_final=3001))


if __name__ == "__main__":
    unittest.main()

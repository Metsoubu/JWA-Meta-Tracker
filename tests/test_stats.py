"""Usage calculation, tier thresholds and rank filters (synthetic data)."""
import unittest

from jwa_tracker import stats
from jwa_tracker.stats import Team


def t(i, rank, creatures, rank_max=None, snap=1):
    return Team(i, snap, rank, rank if rank_max is None else rank_max, frozenset(creatures))


class UsageTests(unittest.TestCase):
    def test_usage_formula(self):
        teams = [t(1, 1, "abcdefgh"), t(2, 2, "abcdefgi"), t(3, 3, "abcdefgj"), t(4, 4, "bcdefghk")]
        counts, total = stats.count_usage(teams)
        self.assertEqual(total, 4)
        self.assertEqual(counts["a"], 3)
        self.assertEqual(stats.percentage(counts["a"], total), 75.0)
        self.assertEqual(stats.percentage(counts["k"], total), 25.0)

    def test_every_roster_creature_listed_including_zero(self):
        creatures = {k: {"key": k, "name": k.upper(), "rarity": "apex", "in_roster": 1} for k in "abcz"}
        rows = stats.usage_table([t(1, 1, "ab"), t(2, 2, "a")], creatures)
        by = {r["key"]: r for r in rows}
        self.assertEqual(set(by), {"a", "b", "c", "z"})
        self.assertEqual(by["z"]["count"], 0)
        self.assertEqual(by["z"]["pct"], 0.0)
        self.assertEqual(by["z"]["tier"], "D")
        self.assertEqual(by["a"]["pct"], 100.0)
        self.assertEqual(by["b"]["pct"], 50.0)
        self.assertEqual(by["a"]["total"], 2)

    def test_sorted_highest_first_then_name(self):
        creatures = {k: {"key": k, "name": k, "in_roster": 1} for k in "abc"}
        rows = stats.usage_table([t(1, 1, "cb"), t(2, 2, "c")], creatures)
        self.assertEqual([r["key"] for r in rows], ["c", "b", "a"])

    def test_unmatched_creatures_only_listed_when_used(self):
        creatures = {"a": {"key": "a", "name": "a", "in_roster": 1}, "x": {"key": "x", "name": "x", "in_roster": 0},
                     "y": {"key": "y", "name": "y", "in_roster": 0}}
        rows = stats.usage_table([t(1, 1, "ax")], creatures)
        self.assertEqual({r["key"] for r in rows}, {"a", "x"})

    def test_empty_sample(self):
        rows = stats.usage_table([], {"a": {"key": "a", "name": "a", "in_roster": 1}})
        self.assertEqual(rows[0]["pct"], 0.0)
        self.assertEqual(rows[0]["total"], 0)


class TierTests(unittest.TestCase):
    def test_thresholds_exact(self):
        cases = [
            (100, 100, "S"), (80, 100, "S"), (7999, 10000, "A"), (60, 100, "A"), (5999, 10000, "B"),
            (40, 100, "B"), (3999, 10000, "C"), (20, 100, "C"), (1999, 10000, "D"), (0, 100, "D"),
        ]
        for count, total, tier in cases:
            with self.subTest(count=count, total=total):
                self.assertEqual(stats.tier_for(count, total), tier)

    def test_no_float_rounding_at_boundary(self):
        # 4/5 = 80% exactly; 799999/1000000 = 79.9999% must stay A.
        self.assertEqual(stats.tier_for(4, 5), "S")
        self.assertEqual(stats.tier_for(799_999, 1_000_000), "A")
        # 1/3 of 3 teams... 2/3 = 66.67% -> A; 1/3 = 33.3% -> C
        self.assertEqual(stats.tier_for(2, 3), "A")
        self.assertEqual(stats.tier_for(1, 3), "C")

    def test_zero_total_is_D(self):
        self.assertEqual(stats.tier_for(0, 0), "D")


class RankFilterTests(unittest.TestCase):
    def setUp(self):
        # one team per exact rank 1..500
        self.teams = [t(i, i, "abcdefgh") for i in range(1, 501)]

    def test_all_filters_select_correct_ranks(self):
        expected = {"top50": 50, "top100": 100, "top250": 250, "top500": 500,
                    "r1-50": 50, "r51-100": 50, "r101-250": 150, "r251-500": 250}
        for key, n in expected.items():
            lo, hi, _ = stats.rank_filter(key)
            inside, straddling = stats.filter_by_rank(self.teams, lo, hi)
            with self.subTest(key=key):
                self.assertEqual(len(inside), n)
                self.assertEqual(straddling, 0)
                self.assertTrue(all(lo <= x.rank_min and x.rank_max <= hi for x in inside))

    def test_bands_of_ten_align_with_brackets(self):
        bands = [t(i, lo, "abcdefgh", rank_max=lo + 9) for i, lo in enumerate(range(1, 100, 10))]
        inside, straddling = stats.filter_by_rank(bands, 1, 50)
        self.assertEqual((len(inside), straddling), (5, 0))
        inside, straddling = stats.filter_by_rank(bands, 51, 100)
        self.assertEqual((len(inside), straddling), (5, 0))

    def test_straddling_band_is_excluded_and_reported(self):
        inside, straddling = stats.filter_by_rank([t(1, 41, "a", rank_max=60)], 1, 50)
        self.assertEqual((len(inside), straddling), (0, 1))

    def test_percentages_recalculate_per_sample(self):
        # creature 'z' only on ranks 1-50
        teams = [t(i, i, "abcdefg" + ("z" if i <= 50 else "y")) for i in range(1, 101)]
        for key, pct in (("top50", 100.0), ("r51-100", 0.0), ("top100", 50.0)):
            lo, hi, _ = stats.rank_filter(key)
            counts, total = stats.count_usage(stats.filter_by_rank(teams, lo, hi)[0])
            with self.subTest(key=key):
                self.assertEqual(stats.percentage(counts.get("z", 0), total), pct)

    def test_unknown_filter(self):
        with self.assertRaises(KeyError):
            stats.rank_filter("top1000")


class CoverageTests(unittest.TestCase):
    def test_complete_partial_none(self):
        self.assertEqual(stats.coverage_summary(1, 100, 100, 100)["state"], "complete")
        partial = stats.coverage_summary(1, 500, 100, 100)
        self.assertEqual(partial["state"], "partial")
        self.assertEqual(partial["ranks_covered"], [1, 100])
        self.assertIn("1–100", partial["message"])
        none = stats.coverage_summary(101, 250, 100, 0)
        self.assertEqual(none["state"], "none")
        self.assertIsNone(none["ranks_covered"])
        self.assertEqual(stats.coverage_summary(1, 50, 0, 0)["state"], "none")


class CompareTests(unittest.TestCase):
    def test_delta_in_percentage_points(self):
        creatures = {k: {"key": k, "name": k, "in_roster": 1} for k in "ab"}
        base = stats.usage_table([t(1, 1, "a"), t(2, 2, "b")], creatures)  # a 50%
        cur = stats.usage_table([t(3, 1, "a"), t(4, 2, "a"), t(5, 3, "a"), t(6, 4, "b")], creatures)  # a 75%
        rows = {r["key"]: r for r in stats.compare(base, cur)}
        self.assertAlmostEqual(rows["a"]["delta_pp"], 25.0)
        self.assertAlmostEqual(rows["b"]["delta_pp"], -25.0)
        self.assertEqual(rows["a"]["base_tier"], "B")
        self.assertEqual(rows["a"]["tier"], "A")

    def test_delta_none_when_one_side_empty(self):
        creatures = {"a": {"key": "a", "name": "a", "in_roster": 1}}
        rows = stats.compare(stats.usage_table([], creatures), stats.usage_table([t(1, 1, "a")], creatures))
        self.assertIsNone(rows[0]["delta_pp"])

    def test_trend_series(self):
        s = stats.trend_series([(1, [t(1, 1, "a"), t(2, 2, "b")]), (2, []), (3, [t(3, 1, "a")])], ["a"])
        self.assertEqual(s["a"], [50.0, None, 100.0])


if __name__ == "__main__":
    unittest.main()

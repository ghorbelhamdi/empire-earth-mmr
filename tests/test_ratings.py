import copy
import json
import math
import random
import unittest
from pathlib import Path

from ratings import (
    MILITARY_VERSION,
    LEGACY_VERSION,
    MAX_MODIFIER_FRACTION,
    MAX_STAT_COUNT,
    OS_DEFAULT_MU,
    OS_DEFAULT_SIGMA,
    OS_MODEL,
    _military_adjustments,
    ordinal_to_mmr,
    predict_win,
    rate_match as versioned_rate_match,
)


def military_v2(winners, losers, stats=None, version=MILITARY_VERSION):
    """Keep the original model regression suite explicitly bound to v2."""
    return versioned_rate_match(winners, losers, stats, version)


def player(name, mu=OS_DEFAULT_MU, sigma=OS_DEFAULT_SIGMA):
    return {"name": name, "mu": mu, "sigma": sigma}


def values(teams):
    return [[(rating.name, rating.mu, rating.sigma) for rating in team] for team in teams]


class RatingsTests(unittest.TestCase):
    def setUp(self):
        self.winners = [player("Alice"), player("Bob")]
        self.losers = [player("Chloe"), player("David")]
        self.stats = {
            "Alice": {"units_killed": 136, "units_lost": 55},
            "Bob": {"units_killed": 91, "units_lost": 55},
            "Chloe": {"units_killed": 93, "units_lost": 175},
            "David": {"units_killed": 16, "units_lost": 52},
        }

    def baseline(self, winners=None, losers=None):
        return OS_MODEL.rate([
            [OS_MODEL.rating(mu=p["mu"], sigma=p["sigma"], name=p["name"])
             for p in team]
            for team in (winners or self.winners, losers or self.losers)
        ], ranks=[0, 1])

    def test_default_mmr_is_existing_1000(self):
        self.assertEqual(ordinal_to_mmr(OS_DEFAULT_MU, OS_DEFAULT_SIGMA), 1000)

    def test_legacy_versions_match_pre_v3_golden_ratings_and_audit_exactly(self):
        winners = [player("Alice", 34, 1), player("Bob", 19, 8)]
        losers = [player("Chloe", 25, 3), player("David", 27, 6)]
        fixtures = json.loads((Path(__file__).parent / "fixtures" / "legacy_ratings.json").read_text(encoding="utf-8"))
        for version, expected in fixtures.items():
            with self.subTest(version=version):
                new_w, new_l, details = versioned_rate_match(winners, losers, self.stats, version)
                actual = {"ratings": [[{"name": r.name, "mu": r.mu, "sigma": r.sigma} for r in team]
                                      for team in (new_w, new_l)], "details": details}
                self.assertEqual(actual, expected)

    def test_no_stats_is_exactly_original_openskill(self):
        for version in (LEGACY_VERSION, MILITARY_VERSION):
            new_w, new_l, details = military_v2(self.winners, self.losers, version=version)
            self.assertEqual(values((new_w, new_l)), values(self.baseline()))
            self.assertFalse(details["performance_applied"])
            self.assertFalse(details["stats_present"])
            self.assertTrue(all(row["performance_mu_delta"] == 0
                                for row in details["players"].values()))

    def test_legacy_ignores_complete_stats_and_retains_them_in_audit(self):
        new_w, new_l, details = military_v2(self.winners, self.losers, self.stats, LEGACY_VERSION)
        self.assertEqual(values((new_w, new_l)), values(self.baseline()))
        self.assertTrue(details["stats_present"])
        self.assertFalse(details["performance_applied"])
        self.assertEqual(details["players"]["Alice"]["units_killed"], 136)

    def test_cap_zero_sum_sigma_and_result_direction(self):
        varied_w = [player("Alice", 34, 1), player("Bob", 19, 8)]
        varied_l = [player("Chloe", 25, 3), player("David", 27, 6)]
        base_teams = self.baseline(varied_w, varied_l)
        new_w, new_l, details = military_v2(varied_w, varied_l, self.stats)
        self.assertTrue(details["performance_applied"])
        for old_team, baseline, changed in zip((varied_w, varied_l), base_teams, (new_w, new_l)):
            self.assertAlmostEqual(sum(r.mu for r in baseline), sum(r.mu for r in changed), places=12)
            for old, base, new in zip(old_team, baseline, changed):
                base_delta = base.mu - old["mu"]
                final_delta = new.mu - old["mu"]
                self.assertEqual(new.sigma, base.sigma)
                self.assertLessEqual(abs(new.mu - base.mu), MAX_MODIFIER_FRACTION * abs(base_delta) + 1e-12)
                self.assertGreaterEqual(base_delta * final_delta, 0)
                self.assertAlmostEqual(details["players"][old["name"]]["final_mu_delta"], final_delta)
        self.assertGreater(details["players"]["Alice"]["performance_mu_delta"], 0)
        self.assertLess(details["players"]["Bob"]["performance_mu_delta"], 0)
        self.assertGreater(details["players"]["Chloe"]["performance_mu_delta"], 0)

    def test_equal_scores_and_all_zero_are_exactly_neutral(self):
        for killed, lost in ((0, 0), (8, 4), (1000, 0)):
            stats = {name: {"units_killed": killed, "units_lost": lost} for name in self.stats}
            new_w, new_l, details = military_v2(self.winners, self.losers, stats)
            self.assertEqual(values((new_w, new_l)), values(self.baseline()))
            self.assertFalse(details["performance_applied"])

    def test_single_player_teams_are_neutral_even_with_extreme_difference(self):
        winners, losers = [self.winners[0]], [self.losers[0]]
        stats = {
            "Alice": {"units_killed": MAX_STAT_COUNT, "units_lost": 0},
            "Chloe": {"units_killed": 0, "units_lost": MAX_STAT_COUNT},
        }
        new_w, new_l, details = military_v2(winners, losers, stats)
        self.assertEqual(values((new_w, new_l)), values(self.baseline(winners, losers)))
        self.assertFalse(details["performance_applied"])

    def test_zero_losses_and_extreme_stats_remain_finite_and_bounded(self):
        stats = {name: {"units_killed": MAX_STAT_COUNT if index % 2 else 0,
                        "units_lost": 0 if index % 2 else MAX_STAT_COUNT}
                 for index, name in enumerate(self.stats)}
        new_w, new_l, details = military_v2(self.winners, self.losers, stats)
        json.dumps(details, allow_nan=False)
        for row in details["players"].values():
            self.assertTrue(math.isfinite(row["military_score"]))
            self.assertLessEqual(abs(row["performance_mu_delta"]),
                                 0.1 * abs(row["base_mu_delta"]) + 1e-12)
        self.assertTrue(all(math.isfinite(r.mu) and math.isfinite(r.sigma) for r in new_w + new_l))

    def test_zero_base_change_has_no_performance_budget(self):
        before = [OS_MODEL.rating(mu=25, sigma=1, name=p["name"]) for p in self.winners]
        adjustments, _ = _military_adjustments(before, before, self.stats)
        self.assertEqual(adjustments, [0, 0])

    def test_incomplete_extra_or_empty_stats_are_rejected(self):
        missing = copy.deepcopy(self.stats)
        del missing["David"]
        extra = copy.deepcopy(self.stats)
        extra["Unknown"] = {"units_killed": 1, "units_lost": 1}
        wrong_case = copy.deepcopy(self.stats)
        wrong_case["alice"] = wrong_case.pop("Alice")
        for stats in ({}, missing, extra, wrong_case, [], "invalid"):
            with self.subTest(stats=stats), self.assertRaises(ValueError):
                military_v2(self.winners, self.losers, stats)

    def test_bad_stat_values_are_rejected_including_in_legacy_mode(self):
        for bad in (-1, MAX_STAT_COUNT + 1, True, False, 1.0, "1", None, float("nan")):
            for field in ("units_killed", "units_lost"):
                stats = copy.deepcopy(self.stats)
                stats["Alice"][field] = bad
                for version in (MILITARY_VERSION, LEGACY_VERSION):
                    with self.subTest(value=bad, field=field, version=version), self.assertRaises(ValueError):
                        military_v2(self.winners, self.losers, stats, version)

    def test_missing_and_unknown_stat_fields_are_rejected(self):
        for bad_row in ({"units_killed": 1}, {"units_killed": 1, "units_lost": 1, "kills": 1}, None):
            stats = copy.deepcopy(self.stats)
            stats["Alice"] = bad_row
            with self.subTest(row=bad_row), self.assertRaises(ValueError):
                military_v2(self.winners, self.losers, stats)

    def test_unknown_version_rejected(self):
        for version in ("typo-v2", None, [], {}):
            with self.subTest(version=version), self.assertRaises(ValueError):
                military_v2(self.winners, self.losers, version=version)

    def test_near_certain_wins_and_upsets_obey_cap_even_at_float_precision(self):
        stats = {name: {"units_killed": MAX_STAT_COUNT if index % 2 else 0,
                        "units_lost": 0 if index % 2 else MAX_STAT_COUNT}
                 for index, name in enumerate(self.stats)}
        for gap in (25, 65.2, 70, 80, 90, 100, 300, 1000):
            for favorite_wins in (True, False):
                winner_mu, loser_mu = (25 + gap, 25 - gap) if favorite_wins else (25 - gap, 25 + gap)
                winners = [player("Alice", winner_mu, .01), player("Bob", winner_mu, 8.33)]
                losers = [player("Chloe", loser_mu, .01), player("David", loser_mu, 8.33)]
                new_w, new_l, details = military_v2(winners, losers, stats)
                base_teams = self.baseline(winners, losers)
                for baseline, changed in zip(base_teams, (new_w, new_l)):
                    self.assertAlmostEqual(math.fsum(r.mu for r in baseline),
                                           math.fsum(r.mu for r in changed), places=11)
                for row in details["players"].values():
                    # Deliberately no tolerance: representable mu must respect
                    # the actual cap, even if a base change is only a few ULPs.
                    self.assertLessEqual(abs(row["performance_mu_delta"]),
                                         MAX_MODIFIER_FRACTION * abs(row["base_mu_delta"]))
                    self.assertGreaterEqual(row["base_mu_delta"] * row["final_mu_delta"], 0)

    def test_empty_duplicate_or_invalid_players_rejected(self):
        for w, l in (([], self.losers), (self.winners, []),
                     ([player("Alice"), player("Alice")], self.losers),
                     (self.winners, [player("Alice")]),
                     ([player(" ")], self.losers),
                     ([player("Alice", sigma=0)], self.losers),
                     ([player("Alice", mu=float("inf"))], self.losers),
                     ([player("Alice", sigma=float("nan"))], self.losers)):
            with self.subTest(winners=w, losers=l), self.assertRaises(ValueError):
                military_v2(w, l)

    def test_missing_or_null_ratings_preserve_defaults(self):
        new_w, new_l, _ = military_v2([{"name": "Alice"}, {"name": "Bob", "mu": None, "sigma": None}], self.losers)
        self.assertEqual(values((new_w, new_l)), values(self.baseline()))

    def test_inputs_are_unchanged(self):
        old_inputs = copy.deepcopy((self.winners, self.losers, self.stats))
        military_v2(self.winners, self.losers, self.stats)
        self.assertEqual((self.winners, self.losers, self.stats), old_inputs)

    def test_repeated_match_and_details_are_deterministic(self):
        new_w, new_l, details = military_v2(self.winners, self.losers, self.stats)
        repeated_w, repeated_l, repeated_details = military_v2(self.winners, self.losers, self.stats)
        self.assertEqual(values((new_w, new_l)), values((repeated_w, repeated_l)))
        self.assertEqual(details, repeated_details)
        json.dumps(details, allow_nan=False)

    def test_chronological_mixed_version_history_replays_identically(self):
        def replay():
            players = {p["name"]: copy.deepcopy(p) for p in self.winners + self.losers}
            audit = []
            matches = [(["Alice", "Bob"], ["Chloe", "David"], LEGACY_VERSION),
                       (["Alice", "Chloe"], ["Bob", "David"], MILITARY_VERSION),
                       (["David", "Bob"], ["Chloe", "Alice"], MILITARY_VERSION)]
            for winners, losers, version in matches:
                new_w, new_l, details = military_v2([players[n] for n in winners], [players[n] for n in losers],
                                                 None if version == LEGACY_VERSION else self.stats, version)
                audit.append(details)
                for rating in new_w + new_l:
                    players[rating.name].update(mu=rating.mu, sigma=rating.sigma)
            return players, audit
        self.assertEqual(replay(), replay())

    def test_property_caps_and_team_conservation_with_varying_uncertainty_and_sizes(self):
        rng = random.Random(123)
        for size_w, size_l in ((1, 2), (2, 2), (2, 3), (4, 4)):
            for _ in range(12):
                teams = [[player(f"{team}-{i}", rng.uniform(5, 45), rng.uniform(0.5, 9))
                          for i in range(size)] for team, size in enumerate((size_w, size_l))]
                stats = {p["name"]: {"units_killed": rng.randint(0, MAX_STAT_COUNT),
                                     "units_lost": rng.randint(0, MAX_STAT_COUNT)} for t in teams for p in t}
                base_teams = self.baseline(*teams)
                new_w, new_l, _ = military_v2(*teams, stats=stats)
                for before, base, changed in zip(teams, base_teams, (new_w, new_l)):
                    self.assertAlmostEqual(sum(r.mu for r in base), sum(r.mu for r in changed), places=11)
                    for old, baseline, new in zip(before, base, changed):
                        base_delta = baseline.mu - old["mu"]
                        self.assertLessEqual(abs(new.mu - baseline.mu), 0.1 * abs(base_delta) + 1e-12)
                        self.assertGreaterEqual(base_delta * (new.mu - old["mu"]), 0)
                        self.assertEqual(new.sigma, baseline.sigma)

    def test_prediction_matches_openskill_and_respects_uncertainty_and_size(self):
        self.assertEqual(predict_win(self.winners, self.losers), (0.5, 0.5))
        strong = [player("Strong", mu=35, sigma=1)]
        weak = [player("Weak", mu=20, sigma=1)]
        p1, p2 = predict_win(strong, weak)
        self.assertGreater(p1, 0.5)
        self.assertAlmostEqual(p1 + p2, 1)
        reverse_p1, reverse_p2 = predict_win(weak, strong)
        self.assertAlmostEqual(p2, reverse_p1)
        self.assertAlmostEqual(p1, reverse_p2)
        self.assertLess(predict_win([player("Strong", mu=35, sigma=9)], weak)[0], p1)
        self.assertLess(predict_win([self.winners[0]], self.losers)[0], 0.5)
        expected = OS_MODEL.predict_win([[OS_MODEL.rating(mu=35, sigma=1)], [OS_MODEL.rating(mu=20, sigma=1)]])
        self.assertEqual((p1, p2), tuple(expected))


if __name__ == "__main__":
    unittest.main()

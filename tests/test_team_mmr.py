"""Public v3 points are independent of OpenSkill's individual uncertainty gains."""
import copy
import json
import math
import random
import unittest

from ratings import (CURRENT_VERSION, LEGACY_VERSION, MILITARY_VERSION,
                     OS_DEFAULT_SIGMA, PUBLIC_MODIFIER_FRACTION, TEAM_K_FACTOR,
                     ordinal_to_mmr, predict_win, rate_match)


def player(name, mu=25.0, sigma=OS_DEFAULT_SIGMA, mmr=1000, **extra):
    return dict(name=name, mu=mu, sigma=sigma, mmr=mmr, **extra)


def rating_values(teams):
    return [[(r.name, r.mu, r.sigma) for r in team] for team in teams]


def stat_rows(teams):
    return {p["name"]: {"units_killed": 1000 if index % 2 == 0 else 0,
                         "units_lost": 0 if index % 2 == 0 else 1000}
            for team in teams for index, p in enumerate(team)}


class TeamMmrTests(unittest.TestCase):
    def setUp(self):
        self.winners = [player("Alice", sigma=8), player("Bob", sigma=1)]
        self.losers = [player("Chloe", sigma=2), player("David", sigma=4)]

    def test_equal_team_result_points_despite_very_different_uncertainty(self):
        new_w, new_l, audit = rate_match(self.winners, self.losers)
        self.assertEqual(audit["version"], CURRENT_VERSION)
        self.assertEqual(audit["winner_probability"], 0.5)
        self.assertEqual([audit["players"][p["name"]]["final_mmr_delta"] for p in self.winners], [24, 24])
        self.assertEqual([audit["players"][p["name"]]["final_mmr_delta"] for p in self.losers], [-24, -24])
        self.assertNotEqual(new_w[0].mu - self.winners[0]["mu"], new_w[1].mu - self.winners[1]["mu"])
        self.assertTrue(all(r.sigma != p["sigma"] for r, p in zip(new_w + new_l, self.winners + self.losers)))

    def test_redistributing_uncertainty_within_team_does_not_redistribute_result_points(self):
        swapped = [dict(self.winners[0], sigma=1), dict(self.winners[1], sigma=8)]
        first = rate_match(self.winners, self.losers)[2]
        second = rate_match(swapped, self.losers)[2]
        self.assertEqual(first["winner_probability"], second["winner_probability"])
        for name in first["players"]:
            self.assertEqual(first["players"][name]["final_mmr_delta"], second["players"][name]["final_mmr_delta"])
        self.assertNotEqual(first["players"]["Alice"]["base_mu_delta"], second["players"]["Alice"]["base_mu_delta"])

    def test_stored_public_balance_is_the_anchor_with_no_ordinal_or_confidence_bonus(self):
        winners = [dict(self.winners[0], mmr=7000), dict(self.winners[1], mmr=-200)]
        audit = rate_match(winners, self.losers)[2]
        self.assertEqual(audit["players"]["Alice"]["new_mmr"], 7024)
        self.assertEqual(audit["players"]["Bob"]["new_mmr"], -176)
        self.assertEqual(audit["players"]["Alice"]["old_mmr"], 7000)
        self.assertNotEqual(audit["players"]["Alice"]["new_mmr"], audit["players"]["Alice"]["new_model_mmr"])
        for row in audit["players"].values():
            self.assertEqual(row["new_mmr"] - row["old_mmr"], row["base_mmr_delta"] + row["performance_mmr_delta"])

    def test_missing_mmr_uses_legacy_ordinal_and_invalid_balances_are_rejected(self):
        winners = [{"name": "Alice", "mu": 30, "sigma": 2}, {"name": "Bob", "mmr": None}]
        audit = rate_match(winners, self.losers)[2]
        self.assertEqual(audit["players"]["Alice"]["old_mmr"], ordinal_to_mmr(30, 2))
        self.assertEqual(audit["players"]["Bob"]["old_mmr"], 1000)
        for bad in (True, False, "1000", 1000.0, float("inf"), float("nan")):
            with self.subTest(value=bad), self.assertRaisesRegex(ValueError, "Public MMR"):
                rate_match([dict(self.winners[0], mmr=bad), self.winners[1]], self.losers)

    def test_internal_model_is_exactly_v2_and_audit_distinguishes_public_twenty_percent(self):
        stats = stat_rows((self.winners, self.losers))
        v2 = rate_match(self.winners, self.losers, stats, MILITARY_VERSION)
        v3 = rate_match(self.winners, self.losers, stats)
        self.assertEqual(rating_values(v2[:2]), rating_values(v3[:2]))
        self.assertEqual(v3[2]["model_version"], MILITARY_VERSION)
        self.assertEqual(v3[2]["model_modifier_fraction"], 0.1)
        self.assertEqual(v3[2]["max_modifier_fraction"], 0.2)
        self.assertEqual(v3[2]["k_factor"], 48)
        for name, row in v2[2]["players"].items():
            for key in ("base_mu_delta", "performance_mu_delta", "final_mu_delta", "military_score"):
                self.assertEqual(v3[2]["players"][name][key], row[key])
            self.assertEqual(v3[2]["players"][name]["old_model_mmr"], row["old_mmr"])
            self.assertEqual(v3[2]["players"][name]["new_model_mmr"], row["new_mmr"])

    def test_military_points_are_visible_bounded_and_do_not_reverse_results(self):
        audit = rate_match(self.winners, self.losers, stat_rows((self.winners, self.losers)))[2]
        self.assertTrue(audit["performance_applied"])
        self.assertEqual([audit["players"][p["name"]]["final_mmr_delta"] for p in self.winners], [28, 20])
        self.assertEqual([audit["players"][p["name"]]["final_mmr_delta"] for p in self.losers], [-20, -28])
        for row in audit["players"].values():
            self.assertEqual(row["performance_mmr_cap"], 4)
            self.assertLessEqual(abs(row["performance_mmr_delta"]), 4)
        self.assertEqual(sum(row["final_mmr_delta"] for row in audit["players"].values()), 0)

    def test_no_stats_equal_stats_and_single_player_teams_have_no_military_points(self):
        for stats in (None, {p["name"]: {"units_killed": 100, "units_lost": 0} for p in self.winners + self.losers}):
            audit = rate_match(self.winners, self.losers, stats)[2]
            self.assertFalse(audit["performance_applied"])
            self.assertTrue(all(row["performance_mmr_delta"] == 0 for row in audit["players"].values()))
        solo_w, solo_l = [self.winners[0]], [self.losers[0]]
        audit = rate_match(solo_w, solo_l, stat_rows((solo_w, solo_l)))[2]
        self.assertFalse(audit["performance_applied"])

    def test_zero_public_cap_can_coexist_with_internal_model_adjustments(self):
        winners = [player("Alice", 30, 1), player("Bob", 30, 1)]
        losers = [player("Chloe", 25, 1), player("David", 25, 1)]
        audit = rate_match(winners, losers, stat_rows((winners, losers)))[2]
        self.assertTrue(audit["model_performance_applied"])
        self.assertFalse(audit["performance_applied"])
        self.assertTrue(all(row["performance_mmr_cap"] == 0 for row in audit["players"].values()))

    def test_unequal_teams_have_common_integer_bases_and_exact_zero_sum(self):
        winners = [player("A", 37.5), player("B", 37.5)]
        losers = [player("C"), player("D"), player("E")]
        audit = rate_match(winners, losers)[2]
        self.assertEqual(audit["winner_probability"], 0.5)
        self.assertEqual(audit["team_mmr_pool"], 48)
        self.assertEqual(audit["team_mmr_quantum"], 6)
        self.assertEqual([audit["players"][p["name"]]["base_mmr_delta"] for p in winners], [24, 24])
        self.assertEqual([audit["players"][p["name"]]["base_mmr_delta"] for p in losers], [-16, -16, -16])

    def test_quantized_extreme_upset_can_exceed_k_and_certain_favorite_can_receive_zero(self):
        winners = [player(f"W{i}", 0, 1) for i in range(4)]
        losers = [player(f"L{i}", 100, 1) for i in range(5)]
        audit = rate_match(winners, losers)[2]
        self.assertEqual(audit["winner_probability"], 0)
        self.assertEqual(audit["team_mmr_pool"], 200)
        self.assertEqual({audit["players"][p["name"]]["base_mmr_delta"] for p in winners}, {50})
        self.assertEqual({audit["players"][p["name"]]["base_mmr_delta"] for p in losers}, {-40})
        favorite = rate_match(losers, winners, stat_rows((losers, winners)))[2]
        self.assertEqual(favorite["winner_probability"], 1)
        self.assertEqual(favorite["team_mmr_pool"], 0)
        self.assertTrue(all(row["final_mmr_delta"] == 0 for row in favorite["players"].values()))

    def test_fractional_ties_are_stable_by_name_and_by_player_id_across_renames(self):
        winners = [player(name) for name in ("Top", "Alice", "Bob", "Carol")]
        losers = [player(f"L{i}") for i in range(4)]
        stats = {p["name"]: {"units_killed": int(p["name"] == "Top"), "units_lost": 0} for p in winners + losers}
        first = rate_match(winners, losers, stats)[2]
        changed = rate_match(list(reversed(winners)), list(reversed(losers)), dict(reversed(list(stats.items()))))[2]
        first_points = {name: row["performance_mmr_delta"] for name, row in first["players"].items()}
        self.assertEqual(first_points, {name: row["performance_mmr_delta"] for name, row in changed["players"].items()})
        self.assertEqual({name: first_points[name] for name in ("Top", "Alice", "Bob", "Carol")}, {"Top": 4, "Alice": -1, "Bob": -1, "Carol": -2})
        with_ids = [dict(p, id=i + 1) for i, p in enumerate(winners)]
        before = rate_match(with_ids, losers, stats)[2]
        renamed = [dict(p, name="Aaron" if p["name"] == "Carol" else p["name"]) for p in with_ids]
        renamed_stats = dict(stats); renamed_stats["Aaron"] = renamed_stats.pop("Carol")
        after = rate_match(renamed, losers, renamed_stats)[2]
        for old, new in zip(with_ids, renamed):
            self.assertEqual(before["players"][old["name"]]["final_mmr_delta"], after["players"][new["name"]]["final_mmr_delta"])

    def test_every_supported_roster_size_preserves_integer_signs_caps_and_total_points(self):
        rng = random.Random(2037)
        for size_w in range(1, 10):
            for size_l in range(1, 11 - size_w):
                for _ in range(4):
                    teams = [[player(f"{label}-{i}", rng.uniform(10, 40), rng.uniform(.5, 9), rng.randint(-1000, 7000), id=offset + i)
                              for i in range(size)] for label, size, offset in (("W", size_w, 0), ("L", size_l, 100))]
                    stats = {p["name"]: {"units_killed": rng.randint(0, 1000000), "units_lost": rng.randint(0, 1000000)} for team in teams for p in team}
                    audit = rate_match(*teams, stats)[2]
                    with self.subTest(winners=size_w, losers=size_l):
                        self.assertEqual(sum(row["final_mmr_delta"] for row in audit["players"].values()), 0)
                        for team, won in zip(teams, (True, False)):
                            rows = [audit["players"][p["name"]] for p in team]
                            self.assertEqual(len({row["base_mmr_delta"] for row in rows}), 1)
                            self.assertEqual(sum(row["performance_mmr_delta"] for row in rows), 0)
                            for p, row in zip(team, rows):
                                self.assertIs(type(row["new_mmr"]), int)
                                self.assertEqual(row["new_mmr"] - p["mmr"], row["final_mmr_delta"])
                                self.assertLessEqual(abs(row["performance_mmr_delta"]), math.floor(abs(row["base_mmr_delta"]) * PUBLIC_MODIFIER_FRACTION))
                                self.assertGreaterEqual(row["final_mmr_delta"] if won else -row["final_mmr_delta"], 0)
                        self.assertEqual(audit["team_mmr_pool"] % math.lcm(size_w, size_l), 0)
                        self.assertEqual(audit["k_factor"], TEAM_K_FACTOR)

    def test_replay_carries_public_mmr_independently_and_is_deterministic(self):
        schedule = [(["Alice", "Bob"], ["Chloe", "David"]), (["Alice", "Chloe"], ["Bob", "David"]),
                    (["David", "Bob"], ["Chloe", "Alice"])] * 3
        original = copy.deepcopy((self.winners, self.losers))
        def replay(versions):
            players = {p["name"]: copy.deepcopy(p) for team in original for p in team}
            audits = []
            for index, (winners, losers) in enumerate(schedule):
                before_total = sum(p["mmr"] for p in players.values())
                teams = [[players[name] for name in names] for names in (winners, losers)]
                version = versions[index % len(versions)]
                new_w, new_l, audit = rate_match(*teams, stat_rows(teams), version)
                audits.append(audit)
                for rating in new_w + new_l:
                    players[rating.name].update(mu=rating.mu, sigma=rating.sigma, mmr=audit["players"][rating.name]["new_mmr"])
                if version == CURRENT_VERSION:
                    self.assertEqual(sum(p["mmr"] for p in players.values()), before_total)
            json.dumps(audits, allow_nan=False)
            return players, audits
        self.assertEqual(replay([CURRENT_VERSION]), replay([CURRENT_VERSION]))
        self.assertEqual(sum(p["mmr"] for p in replay([CURRENT_VERSION])[0].values()), 4000)
        self.assertEqual(replay([LEGACY_VERSION, MILITARY_VERSION, CURRENT_VERSION]), replay([LEGACY_VERSION, MILITARY_VERSION, CURRENT_VERSION]))
        self.assertEqual((self.winners, self.losers), original)


if __name__ == "__main__":
    unittest.main()

"""Versioned, deterministic rating calculations, independent of Flask and the DB.

``openskill-v1`` is the existing Plackett-Luce win/loss update. ``military-v2``
uses exactly that update as its baseline, then optionally redistributes a small
amount of mu *within each team*. All players' post-match sigma values are kept.

For complete, validated military statistics, player i has:

    s_i = log(1 + units_killed_i) - log(1 + units_lost_i)
    c_i = 0.10 * abs(baseline_mu_i - previous_mu_i)
    mean_s = sum(c_i * s_i) / sum(c_i)
    d = max(1, max(abs(s_i - mean_s)))
    adjustment_i = c_i * (s_i - mean_s) / d

The mean and denominator are calculated separately for each team. Consequently
the adjustments sum to zero within each team, each adjustment is bounded by
10% of that player's baseline mu change, and a baseline gain/loss cannot reverse
direction. The cap-weighted mean is necessary when uncertainty differs between
teammates: an unweighted mean followed by individual clipping is not zero-sum.
Single-player teams, identical scores, and teams whose caps are all zero are
neutral. No statistics means exactly the v1 calculation, without extra matches.

This 10% setting is a conservative product choice, not an empirically calibrated
Empire Earth skill model. Raw kills/losses do not measure unit costs, economy,
support play, victory conditions, or match length. Statistics should be reviewed
before approval. Log smoothing handles zero losses and limits outliers; it does
not make an OCR capture or a self-reported result trustworthy. Keep the version,
statistics, and OpenSkill dependency version when replaying history.

Display MMR retains the existing conservative ordinal. Since sigma can decrease
after a loss, *display MMR* may increase even when mu decreases. The military
guarantees concern mu; integer MMR rounding need not be zero-sum.
Floating-point mu rounding is directed back toward the baseline if the nearest
representable value would breach the cap; team conservation holds to floating
precision. This matters only for adjustments near the precision of a float.
"""

import math
from collections.abc import Mapping

from openskill.models import PlackettLuce


OS_MODEL = PlackettLuce()
OS_DEFAULT_MU = 25.0
OS_DEFAULT_SIGMA = 25.0 / 3.0
RATING_SCALE = 40.0
RATING_OFFSET = 1000.0
LEGACY_VERSION = "openskill-v1"
CURRENT_VERSION = "military-v2"
SUPPORTED_VERSIONS = frozenset((LEGACY_VERSION, CURRENT_VERSION))
MAX_MODIFIER_FRACTION = 0.10
MAX_STAT_COUNT = 1_000_000


def ordinal_to_mmr(mu, sigma):
    """Preserve the existing display mapping; fresh ratings start at 1000."""
    return round((mu - 3.0 * sigma) * RATING_SCALE + RATING_OFFSET)


def _player_rating(player):
    if not isinstance(player, Mapping):
        raise ValueError("Each player must be a mapping containing a canonical name.")
    name = player.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Every player needs a non-empty canonical name.")
    mu = player.get("mu")
    sigma = player.get("sigma")
    try:
        mu = OS_DEFAULT_MU if mu is None else float(mu)
        sigma = OS_DEFAULT_SIGMA if sigma is None else float(sigma)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Player mu and sigma must be finite numbers.") from exc
    if not math.isfinite(mu) or not math.isfinite(sigma) or sigma <= 0:
        raise ValueError("Player mu must be finite and sigma must be finite and positive.")
    return OS_MODEL.rating(mu=mu, sigma=sigma, name=name)


def _teams(team1, team2):
    ratings1 = [_player_rating(player) for player in team1]
    ratings2 = [_player_rating(player) for player in team2]
    if not ratings1 or not ratings2:
        raise ValueError("A match needs at least one player on each team.")
    names = [rating.name for rating in ratings1 + ratings2]
    if len(names) != len(set(names)):
        raise ValueError("A player may appear only once in a match.")
    return ratings1, ratings2


def _validate_stats(stats, names):
    if stats is None:
        return None
    if not isinstance(stats, Mapping) or set(stats) != set(names):
        raise ValueError("Military stats must cover every match player exactly once.")
    clean = {}
    for name in names:
        row = stats[name]
        if not isinstance(row, Mapping) or set(row) != {"units_killed", "units_lost"}:
            raise ValueError("Each military stats row needs units_killed and units_lost only.")
        clean[name] = {}
        for field in ("units_killed", "units_lost"):
            value = row[field]
            # bool is a Python int subclass, but is never a valid game counter.
            if type(value) is not int or not 0 <= value <= MAX_STAT_COUNT:
                raise ValueError(f"{field} must be an integer from 0 to {MAX_STAT_COUNT}.")
            clean[name][field] = value
    return clean


def _military_adjustments(before, baseline, stats):
    """Return bounded, zero-sum mu adjustments and log scores for one team."""
    scores = [
        math.log1p(stats[r.name]["units_killed"])
        - math.log1p(stats[r.name]["units_lost"])
        for r in before
    ]
    caps = [MAX_MODIFIER_FRACTION * abs(new.mu - old.mu)
            for old, new in zip(before, baseline)]
    total_cap = math.fsum(caps)
    if len(before) == 1 or total_cap == 0 or max(scores) == min(scores):
        return [0.0] * len(before), scores
    weighted_mean = math.fsum(cap * score for cap, score in zip(caps, scores)) / total_cap
    centered = [score - weighted_mean for score in scores]
    denominator = max(1.0, max(abs(value) for value in centered))
    adjustments = [cap * value / denominator for cap, value in zip(caps, centered)]
    return adjustments, scores


def rate_match(w_players, l_players, stats=None, version=CURRENT_VERSION):
    """Return winner ratings, loser ratings, and JSON-serializable audit details.

    Players are mappings with ``name``, optional ``mu``, and optional ``sigma``.
    Missing/null ratings use the original defaults. ``stats`` is either None or
    ``{canonical_name: {"units_killed": int, "units_lost": int}}`` for *every*
    player in the match. Partial/invalid statistics and unknown versions raise
    ValueError. Supplied v1 statistics are validated and retained in the details
    but do not change the rating, so historical v1 matches replay as before.

    Inputs are never mutated. Outputs remain standard OpenSkill Rating objects.
    The audit omits random OpenSkill object IDs so it is replay deterministic.
    """
    if not isinstance(version, str) or version not in SUPPORTED_VERSIONS:
        raise ValueError(f"Unsupported rating version: {version!r}.")
    before_w, before_l = _teams(w_players, l_players)
    clean_stats = _validate_stats(stats, [r.name for r in before_w + before_l])
    new_w, new_l = OS_MODEL.rate([before_w, before_l], ranks=[0, 1])
    details = {
        "version": version,
        "stats_present": clean_stats is not None,
        "performance_applied": False,
        "max_modifier_fraction": MAX_MODIFIER_FRACTION if version == CURRENT_VERSION else 0.0,
        "players": {},
    }
    for before, baseline in ((before_w, new_w), (before_l, new_l)):
        if version == CURRENT_VERSION and clean_stats is not None:
            adjustments, scores = _military_adjustments(before, baseline, clean_stats)
        else:
            adjustments, scores = [0.0] * len(before), [None] * len(before)
        for old, new, adjustment, score in zip(before, baseline, adjustments, scores):
            base_mu = new.mu
            base_delta = base_mu - old.mu
            cap = MAX_MODIFIER_FRACTION * abs(base_delta)
            adjusted_mu = base_mu + adjustment
            if abs(adjusted_mu - base_mu) > cap:
                # Nearest-float rounding can otherwise exceed a tiny cap when a
                # heavily favored player wins. Move one ULP toward the baseline.
                adjusted_mu = math.nextafter(adjusted_mu, base_mu)
            new.mu = adjusted_mu
            # Record the actual representable adjustment after floating arithmetic.
            final_delta = new.mu - old.mu
            actual_adjustment = new.mu - base_mu
            row = {
                "base_mu_delta": base_delta,
                "performance_mu_delta": actual_adjustment,
                "final_mu_delta": final_delta,
                "military_score": score,
                "units_killed": None,
                "units_lost": None,
                "old_mmr": ordinal_to_mmr(old.mu, old.sigma),
                "new_mmr": ordinal_to_mmr(new.mu, new.sigma),
            }
            if clean_stats is not None:
                row.update(clean_stats[old.name])
            details["players"][old.name] = row
            details["performance_applied"] |= actual_adjustment != 0.0
    return new_w, new_l, details


def predict_win(team1, team2):
    """Return (team1 probability, team2 probability), including uncertainty/size.

    Balance candidate teams by minimizing abs(predict_win(t1, t2)[0] - 0.5).
    These are model estimates, not calibrated Empire Earth win probabilities.
    """
    ratings1, ratings2 = _teams(team1, team2)
    probability1, probability2 = OS_MODEL.predict_win([ratings1, ratings2])
    return probability1, probability2

"""
parlay.py

Odds math + parlay pricing logic for the EdgeFinder prop dashboard.

- American <-> implied probability conversions
- Independent cross-game parlay combination
- Same-game parlay (SGP) correlation adjustment, per the rule discussed in
  the original design conversation: when 2+ selected legs share the same
  game, apply a positive correlation multiplier (default 1.15x, adjustable
  1.05x-1.25x in the UI) to that group's joint probability before combining
  across games.
"""

from __future__ import annotations

from typing import Iterable, Sequence

import pandas as pd


def american_to_prob(odds: float) -> float:
    """Convert American odds to implied probability (0-1)."""
    odds = float(odds)
    if odds > 0:
        return 100.0 / (odds + 100.0)
    return abs(odds) / (abs(odds) + 100.0)


def prob_to_american(prob: float) -> int:
    """Convert a probability (0-1) back to American odds."""
    prob = min(max(float(prob), 1e-6), 0.999999)
    if prob <= 0.5:
        return int(round(100.0 * (1.0 - prob) / prob))
    return int(round(-100.0 * prob / (1.0 - prob)))


def format_american(odds: int) -> str:
    return f"+{odds}" if odds > 0 else str(odds)


def _game_key(row: pd.Series) -> tuple:
    """
    Identify which 'game' a leg belongs to, so same-game legs can be grouped
    for correlation pricing. Uses game time + the unordered pair of
    team/opponent so it doesn't matter which side of the matchup a given
    leg's player is on.
    """
    team = str(row.get("Team", "")).strip().upper()
    opponent = str(row.get("Opponent", "")).strip().upper()
    game_time = row.get("GameTime", "")
    matchup = frozenset({team, opponent}) if opponent else frozenset({team})
    return (str(game_time), matchup)


def price_parlay(
    selected: pd.DataFrame,
    odds_col: str = "ModelFairOdds",
    correlation_multiplier: float = 1.15,
) -> dict:
    """
    Compute combined parlay probability + American odds for a set of
    selected legs.

    Independent (different-game) legs: probabilities multiply directly.
    Same-game legs: the group's joint probability gets boosted by
    `correlation_multiplier` before being folded into the overall product,
    reflecting that outcomes like a QB Anytime TD and his TE's Anytime TD
    are positively correlated within one game (red-zone efficiency, game
    script, etc.) rather than independent.

    Returns a dict with the combined probability, American odds, the
    per-game breakdown, and which games (if any) received the SGP boost.
    """
    if selected is None or selected.empty:
        return {
            "n_legs": 0,
            "combined_probability": None,
            "combined_american": None,
            "groups": [],
        }

    working = selected.copy()
    working["_game_key"] = working.apply(_game_key, axis=1)

    groups = []
    total_prob = 1.0

    for key, group_df in working.groupby("_game_key", sort=False):
        leg_probs = [american_to_prob(o) for o in group_df[odds_col]]
        group_prob = 1.0
        for p in leg_probs:
            group_prob *= p

        correlated = len(group_df) > 1
        if correlated:
            group_prob = min(group_prob * correlation_multiplier, 0.999999)

        total_prob *= group_prob

        groups.append(
            {
                "game_key": key,
                "players": group_df["Player"].tolist() if "Player" in group_df else [],
                "n_legs": len(group_df),
                "correlated": correlated,
                "correlation_multiplier": correlation_multiplier if correlated else 1.0,
                "group_probability": group_prob,
                "group_american": prob_to_american(group_prob),
            }
        )

    total_prob = min(max(total_prob, 1e-6), 0.999999)

    return {
        "n_legs": int(len(working)),
        "combined_probability": total_prob,
        "combined_american": prob_to_american(total_prob),
        "groups": groups,
    }

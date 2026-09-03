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


def _uses_loose_line_matching(prop_type: str) -> bool:
    """True for props where different sportsbooks routinely post different
    numbers for what's still "the same bet" in spirit -- yardage props
    (e.g. Bet365 Under 78.5, DraftKings Under 76.5), Passing TDs, and
    Receptions. Anytime TD keeps exact-line matching, since its line is
    essentially always a fixed 0.5 and an exact match there is meaningful
    (and this check is deliberately narrow -- "passing td" -- so it
    doesn't also loosen "Anytime TD" props via a bare "td" substring)."""
    text = (prop_type or "").lower()
    return "yards" in text or "passing td" in text or "reception" in text


def best_book_parlay(
    all_props: pd.DataFrame,
    selected: pd.DataFrame,
    odds_col: str = "SportsbookOdds",
) -> dict:
    """
    Find which single sportsbook -- if any -- carries every one of the
    selected legs, and among those, which offers the best combined payout.

    Each selected leg is matched back to `all_props` (the full slate, not
    just what's currently checked) by (Player, PropType, CoversLine) --
    i.e. "the exact same bet" -- to see what every sportsbook prices that
    leg at. A book only qualifies if it has a price for ALL selected legs;
    among qualifying books, the one with the lowest combined implied
    probability (highest payout) is "best". No same-game correlation
    adjustment is applied here -- this reflects an actual bookmaker payout,
    not a model estimate of true win probability.

    Yardage props, Passing TDs, and Receptions (see _uses_loose_line_matching)
    match more loosely -- by (Player, PropType) only, ignoring the exact
    CoversLine -- since real sportsbooks commonly post different lines for
    the same market (e.g. Under 78.5 at one book, Under 76.5 at another),
    and requiring an exact number match there would make "which book has
    my whole parlay" almost never find a hit. When more than one line is
    available at a book for a loosely-matched leg, the one closest to the
    selected leg's own line is used. Anytime TD still requires an exact
    CoversLine match, since its line is essentially fixed at 0.5 and an
    exact match there is meaningful.

    Returns {"has_complete_book": bool, "best": {...} | None,
    "all_books": [...]} -- all_books is sorted best-to-worst payout among
    only the books that could cover every leg.
    """
    if selected is None or selected.empty or all_props is None or all_props.empty:
        return {"has_complete_book": False, "best": None, "all_books": []}

    required_cols = {"Player", "PropType", "CoversLine", "Sportsbook", odds_col}
    if not required_cols.issubset(all_props.columns):
        return {"has_complete_book": False, "best": None, "all_books": []}

    leg_keys = list(
        selected[["Player", "PropType", "CoversLine"]].itertuples(index=False, name=None)
    )

    results = []
    for book in sorted(all_props["Sportsbook"].dropna().unique().tolist()):
        book_df = all_props[all_props["Sportsbook"] == book]

        odds_for_legs = []
        complete = True
        for player, prop_type, line in leg_keys:
            candidates = book_df[(book_df["Player"] == player) & (book_df["PropType"] == prop_type)]
            if not _uses_loose_line_matching(prop_type):
                candidates = candidates[candidates["CoversLine"] == line]

            if candidates.empty:
                complete = False
                break

            if len(candidates) > 1:
                closest_idx = (candidates["CoversLine"] - line).abs().idxmin()
                match = candidates.loc[[closest_idx]]
            else:
                match = candidates
            odds_for_legs.append(match.iloc[0][odds_col])

        if not complete:
            continue

        prob = 1.0
        for o in odds_for_legs:
            prob *= american_to_prob(o)
        prob = min(max(prob, 1e-6), 0.999999)

        results.append(
            {
                "sportsbook": book,
                "combined_probability": prob,
                "combined_american": prob_to_american(prob),
            }
        )

    results.sort(key=lambda r: r["combined_probability"])  # lowest prob = biggest payout = best

    return {
        "has_complete_book": len(results) > 0,
        "best": results[0] if results else None,
        "all_books": results,
    }

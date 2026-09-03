"""
model.py

Fair-odds / edge estimation for the EdgeFinder prop dashboard.

Real de-vigging needs BOTH sides of a market (Over and Under) priced by the
SAME book, so the vig can be backed out directly from the pair. Covers.com's
one-pick-per-card layout doesn't give us that -- each card shows one side's
price per book, not both -- so this module uses two practical stand-ins
instead, applied per (Player, PropType, CoversLine) group. That grouping is
"the exact same bet": PropType already has the side baked in (e.g.
"Receiving Yards (Over)"), courtesy of how scraper.py builds it.

1. Multi-source consensus (2+ sources price the identical bet): average
   their implied probabilities. Different books/markets set their vig
   independently of each other, so averaging several independent prices is
   a reasonable -- though not exact -- stand-in for the vig-free "true"
   probability: each source's individual overpricing partially cancels out
   against the others instead of all pointing the same direction.

2. Single-source haircut (only 1 source prices the bet): there's nothing to
   average against, so a fixed assumed single-side vig
   (ASSUMED_SINGLE_SIDE_VIG, default 4.5% -- roughly what a standard
   -110 / -110 two-sided market carries in total) is halved and subtracted
   from that one source's implied probability. This is a documented
   assumption, not a measurement -- it exists so a single-book prop still
   gets *some* fair-value estimate instead of just echoing the book's own
   (vig-inflated) price back as "fair," which would make every such prop
   show exactly 0% edge and defeat the point of the tool.

ModelFairOdds is the SAME for every row in a group -- it's the model's one
estimate of that bet's true fair price. EdgePct is computed per ROW, since
different sources can (and do) post different odds for the literal same
bet -- it's the % difference between the group's fair probability and that
specific row's own implied probability from its SportsbookOdds:

    EdgePct = (fair_prob / row_implied_prob - 1) * 100

Positive EdgePct means the row's posted odds imply a lower probability than
the model's fair estimate -- i.e. you're being paid as if the bet were less
likely than the model thinks it actually is, which is a good price for the
bettor. Negative means the opposite (a worse-than-fair price).

This is used identically for live Covers.com data (scraper.py) and for the
bundled sample data (sample_data.py), so "Edge %" means the same thing
regardless of which data source is currently active.
"""

from __future__ import annotations

import pandas as pd

from parlay import american_to_prob, prob_to_american

# Assumed total vig for a standard two-sided market (e.g. -110 / -110,
# which implies roughly 104.8% combined probability -- about 4.5% over
# 100%). Only used as a fallback when a bet has just one priced source to
# work with; halved because that single side only carries half of the
# market's total vig.
ASSUMED_SINGLE_SIDE_VIG = 0.045

# A bet's identity for modeling purposes: same player, same market/side
# (PropType already encodes Over/Under), same line.
_GROUP_COLS = ["Player", "PropType", "CoversLine"]


def estimate_fair_odds(df: pd.DataFrame) -> pd.DataFrame:
    """Return a copy of df with ModelFairOdds and EdgePct recomputed from
    the actual priced data on each row, replacing whatever placeholder
    values the caller put there.

    Rows missing any of the grouping columns or SportsbookOdds are returned
    unchanged apart from ModelFairOdds defaulting to their own
    SportsbookOdds and EdgePct to 0.0 -- "no edge estimate available"
    rather than a guess."""
    if df.empty:
        return df
    required = set(_GROUP_COLS) | {"SportsbookOdds"}
    if not required.issubset(df.columns):
        return df

    working = df.copy()
    working["ModelFairOdds"] = working["SportsbookOdds"]
    working["EdgePct"] = 0.0

    # Group via .groups (index labels per group) rather than .apply() --
    # .apply() on a groupby excludes the groupby columns themselves from the
    # sub-frame it hands to the function, which has bitten this project
    # before (see best_line_per_prop() in app.py). Reading row labels back
    # out with .loc sidesteps that entirely.
    groups = working.groupby(_GROUP_COLS, dropna=False, sort=False).groups
    for _, row_labels in groups.items():
        group = working.loc[row_labels]
        implied_probs = group["SportsbookOdds"].apply(american_to_prob)

        if len(group) >= 2:
            fair_prob = float(implied_probs.mean())
        else:
            fair_prob = float(implied_probs.iloc[0]) - (ASSUMED_SINGLE_SIDE_VIG / 2.0)

        fair_prob = min(max(fair_prob, 0.01), 0.98)
        fair_odds = prob_to_american(fair_prob)

        for row_label, implied_prob in zip(group.index, implied_probs):
            edge_pct = (fair_prob / implied_prob - 1.0) * 100.0 if implied_prob > 0 else 0.0
            working.at[row_label, "ModelFairOdds"] = fair_odds
            working.at[row_label, "EdgePct"] = round(edge_pct, 1)

    return working


if __name__ == "__main__":
    # Quick manual sanity check -- run `python model.py` to see it work.
    sample = pd.DataFrame(
        [
            # Two books price the identical bet -- consensus averaging.
            {"Player": "Test Player", "PropType": "Receiving Yards (Over)", "CoversLine": 78.5, "SportsbookOdds": -110},
            {"Player": "Test Player", "PropType": "Receiving Yards (Over)", "CoversLine": 78.5, "SportsbookOdds": -105},
            # Only one source -- single-source vig haircut.
            {"Player": "Solo Player", "PropType": "Rushing Yards (Under)", "CoversLine": 60.5, "SportsbookOdds": -120},
        ]
    )
    print(estimate_fair_odds(sample).to_string(index=False))

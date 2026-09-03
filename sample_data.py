"""
sample_data.py

Fallback / demo prop data used whenever the live Covers.com scraper
(scraper.py) returns no rows -- e.g. because the sportsbooks haven't posted
lines for the upcoming slate yet, or because Covers' markup changed and the
selectors need a refresh.

IMPORTANT: This is illustrative sample data for building/testing the
dashboard, NOT real, current sportsbook odds. Player/team info reflects
rosters as commonly known as of mid-2025 and may be stale by the time you
run this -- always verify against the live source before using this for
real betting decisions. Lines/odds below are synthetically generated for
demo purposes only -- but ModelFairOdds/EdgePct are computed by the exact
same model.py used for live data (multi-source consensus per prop, since
each prop here is priced at 2-4 sportsbooks -- see get_sample_data), so the
"Edge %" column behaves identically to how it works with real Covers.com
data.
"""

from __future__ import annotations

import random

import pandas as pd

from model import estimate_fair_odds

# Fixed seed so the demo dataset is stable/reproducible between runs.
_RNG = random.Random(42)

SPORTSBOOKS = ["DraftKings", "BetMGM", "Bet365", "theScore Bet"]
INJURY_STATUSES = ["Healthy", "Healthy", "Healthy", "Probable", "Questionable", "Doubtful"]

# (League, Position, Player, Team, Opponent, HomeAway, GameTime, PropType, Line, BaseOdds)
_RAW_ROWS = [
    # --- NFL ---
    ("NFL", "WR", "Justin Jefferson", "MIN", "GB", "vs", "2026-09-06 13:00", "Receiving Yards", 84.5, -115),
    ("NFL", "WR", "Ja'Marr Chase", "CIN", "NE", "vs", "2026-09-07 13:00", "Receiving Yards", 79.5, +140),
    ("NFL", "WR", "CeeDee Lamb", "DAL", "PHI", "@", "2026-09-04 20:20", "Receptions", 6.5, -120),
    ("NFL", "WR", "Amon-Ra St. Brown", "DET", "GB", "vs", "2026-09-06 13:00", "Receiving Yards", 74.5, -110),
    ("NFL", "WR", "Puka Nacua", "LAR", "HOU", "vs", "2026-09-06 16:05", "Anytime TD", 0.5, +135),
    ("NFL", "WR", "Nico Collins", "HOU", "LAR", "@", "2026-09-06 16:05", "Receiving Yards", 69.5, -108),
    ("NFL", "RB", "Christian McCaffrey", "SF", "SEA", "@", "2026-09-06 16:25", "Receptions", 4.5, -135),
    ("NFL", "RB", "Bijan Robinson", "ATL", "TB", "vs", "2026-09-07 13:00", "Rushing Yards", 68.5, -112),
    ("NFL", "RB", "Jahmyr Gibbs", "DET", "GB", "vs", "2026-09-06 13:00", "Anytime TD", 0.5, +150),
    ("NFL", "RB", "Saquon Barkley", "PHI", "DAL", "vs", "2026-09-04 20:20", "Rushing Yards", 91.5, -118),
    ("NFL", "RB", "Derrick Henry", "BAL", "BUF", "@", "2026-09-07 20:20", "Anytime TD", 0.5, +105),
    ("NFL", "RB", "Josh Jacobs", "GB", "DET", "@", "2026-09-06 13:00", "Rushing Yards", 64.5, -110),
    ("NFL", "TE", "Travis Kelce", "KC", "LAC", "vs", "2026-09-05 20:15", "Anytime TD", 0.5, +145),
    ("NFL", "TE", "Sam LaPorta", "DET", "GB", "vs", "2026-09-06 13:00", "Receptions", 4.5, -105),
    ("NFL", "TE", "Trey McBride", "ARI", "NO", "vs", "2026-09-06 16:05", "Receiving Yards", 54.5, -110),
    ("NFL", "TE", "George Kittle", "SF", "SEA", "@", "2026-09-06 16:25", "Anytime TD", 0.5, +160),
    ("NFL", "QB", "Patrick Mahomes", "KC", "LAC", "vs", "2026-09-05 20:15", "Passing Yards", 267.5, -112),
    ("NFL", "QB", "Patrick Mahomes", "KC", "LAC", "vs", "2026-09-05 20:15", "Anytime TD", 0.5, +350),
    ("NFL", "QB", "Josh Allen", "BUF", "BAL", "vs", "2026-09-07 20:20", "Passing + Rushing Yards", 271.5, -115),
    ("NFL", "QB", "Lamar Jackson", "BAL", "BUF", "@", "2026-09-07 20:20", "Rushing Yards", 47.5, -108),
    ("NFL", "QB", "Joe Burrow", "CIN", "NE", "vs", "2026-09-07 13:00", "Passing TDs", 1.5, +120),
    ("NFL", "QB", "Jalen Hurts", "PHI", "DAL", "vs", "2026-09-04 20:20", "Rushing Yards", 38.5, -110),
    # --- CFB (illustrative placeholder matchups -- verify current rosters) ---
    ("CFB", "RB", "Ollie Gordon II", "OKST", "UTAH", "@", "2026-09-05 15:30", "Receptions", 2.5, -110),
    ("CFB", "WR", "Tetairoa McMillan Jr.", "ARIZ", "UTAH", "@", "2026-09-05 22:00", "Receiving Yards", 91.5, +110),
    ("CFB", "QB", "Dylan Raiola", "NEB", "MICH", "vs", "2026-09-06 16:00", "Passing Yards", 245.5, -105),
    ("CFB", "WR", "Carnell Tate", "OSU", "TXAM", "vs", "2026-09-06 19:30", "Receiving Yards", 88.5, +130),
    ("CFB", "RB", "Jeremiyah Love", "ND", "MIA", "vs", "2026-09-06 19:30", "Rushing Yards", 79.5, -115),
    ("CFB", "TE", "Max Klare", "OHIO", "TEX", "@", "2026-09-06 12:00", "Anytime TD", 0.5, +180),
    ("CFB", "WR", "Antonio Williams", "CLEM", "LSU", "vs", "2026-09-06 15:30", "Receiving Yards", 76.5, +145),
    ("CFB", "QB", "Fernando Mendoza", "IND", "OSU", "@", "2026-09-06 12:00", "Passing TDs", 1.5, -120),
]


def _jitter_odds(base_odds: int) -> int:
    """
    Simulate one sportsbook's own price for a prop, as a small random
    variation around a 'consensus' base price -- real books rarely agree to
    the penny. Keeps the same side of the line (won't flip a favorite into
    an underdog or vice versa).
    """
    delta = _RNG.randint(-15, 15)
    jittered = base_odds + delta
    if base_odds > 0 and jittered <= 0:
        jittered = 100
    elif base_odds < 0 and jittered >= 0:
        jittered = -100
    return jittered


def get_sample_data(leagues: list[str] | None = None) -> pd.DataFrame:
    """
    Build the demo DataFrame, optionally restricted to a subset of leagues
    (e.g. ["NFL"] or ["CFB"]). Matches the schema produced by scraper.py so
    the app can treat live and sample data identically.

    Each prop is priced at 2-4 of the sportsbooks in SPORTSBOOKS (not just
    one) -- exactly like scraper.py does with real Covers.com data -- so
    that "which single sportsbook has every leg in my parlay" has real
    cross-book data to compare against instead of each prop only ever
    existing at one, randomly-assigned book.
    """
    rows = []
    for (
        league,
        position,
        player,
        team,
        opponent,
        home_away,
        game_time,
        prop_type,
        line,
        base_odds,
    ) in _RAW_ROWS:
        if leagues and league not in leagues:
            continue

        injury = INJURY_STATUSES[_RNG.randrange(len(INJURY_STATUSES))]
        # Skill-position injuries are more newsworthy than steady vets;
        # keep most rows healthy but leave some variety in for realism.

        # "Game" is always displayed as "Away @ Home" -- home_away tells us
        # whether the player's own Team is the home or away side.
        if home_away == "@":
            away_team, home_team = team, opponent
        else:
            away_team, home_team = opponent, team
        game = f"{away_team} @ {home_team}"

        # Which books carry this prop -- weighted toward 2-3 books so it's
        # common (but not universal) for a prop to be missing from any one
        # given book, same as real sportsbooks.
        available_books = list(SPORTSBOOKS)
        _RNG.shuffle(available_books)
        n_books = _RNG.choice([2, 2, 3, 3, 4])
        available_books = available_books[:n_books]

        for sportsbook in available_books:
            book_odds = _jitter_odds(base_odds)

            rows.append(
                {
                    "League": league,
                    "Position": position,
                    "Player": player,
                    "Team": team,
                    "Opponent": opponent,
                    "Game": game,
                    "GameTime": game_time,
                    "PropType": prop_type,
                    "CoversLine": line,
                    "SportsbookOdds": book_odds,
                    "Sportsbook": sportsbook,
                    "ModelFairOdds": book_odds,  # placeholder -- estimate_fair_odds() fills this in below
                    "EdgePct": 0.0,
                    "InjuryStatus": injury,
                    "DataSource": "SAMPLE",
                }
            )

    df = pd.DataFrame(rows)
    if not df.empty:
        df["GameTime"] = pd.to_datetime(df["GameTime"])
        # Same consensus-or-vig-haircut model used for live Covers.com data
        # (see model.py) -- each prop here is already priced at 2-4 books
        # (available_books above), so most rows get real multi-source
        # consensus rather than the single-source haircut.
        df = estimate_fair_odds(df)
    return df


if __name__ == "__main__":
    d = get_sample_data()
    print(f"Generated {len(d)} sample rows across leagues: {sorted(d['League'].unique())}")
    print(d.head(10).to_string(index=False))

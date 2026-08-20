"""
EdgeFinder: NFL + CFB Player Prop Dashboard & Parlay Builder

Pulls player prop lines (live from Covers.com when available, falling back
to bundled sample data otherwise), filters out WR/RB longshots priced worse
than +125, lets you filter/sort the slate, and prices a live parlay ticket
-- including a same-game correlation boost -- as you check rows.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

import sample_data
import scraper
from parlay import format_american, price_parlay

st.set_page_config(page_title="EdgeFinder: Prop Dashboard & Parlay Builder", layout="wide")

DISPLAY_COLUMNS = [
    "Select",
    "GameTime",
    "League",
    "Position",
    "Player",
    "Team",
    "Matchup",
    "PropType",
    "CoversLine",
    "SportsbookOdds",
    "Sportsbook",
    "ModelFairOdds",
    "EdgePct",
    "InjuryStatus",
]

COLUMN_LABELS = {
    "GameTime": "Game Time",
    "CoversLine": "Covers Line",
    "SportsbookOdds": "Sportsbook Odds",
    "ModelFairOdds": "Model Fair Odds",
    "EdgePct": "Edge %",
    "PropType": "Prop Type",
    "InjuryStatus": "Injury Status",
}


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------
@st.cache_data(ttl=900, show_spinner="Fetching the latest props...")
def load_props_data() -> tuple[pd.DataFrame, bool]:
    """Returns (dataframe, is_live). Tries the Covers.com scraper first;
    falls back to bundled sample data if nothing comes back (e.g. lines
    aren't posted yet for the upcoming slate)."""
    try:
        live_df = scraper.fetch_all_props()
    except Exception:
        live_df = pd.DataFrame()

    if live_df is not None and not live_df.empty:
        return live_df, True

    return sample_data.get_sample_data(), False


def apply_longshot_filter(df: pd.DataFrame, enforce: bool) -> pd.DataFrame:
    """Enforces that WR/RB props priced above +125 are dropped, while QB/TE
    pricing stays unrestricted -- the core filtering rule from the spec."""
    if not enforce or df.empty:
        return df
    is_longshot_skill_position = df["Position"].isin(["WR", "RB"]) & (df["SportsbookOdds"] > 125)
    return df[~is_longshot_skill_position].reset_index(drop=True)


# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------
st.title("🏈 EdgeFinder: NFL + CFB Prop Dashboard")
st.caption(
    "Filter the slate, sort by edge/odds/position/kickoff, check boxes to build a parlay, "
    "and see live combined odds with same-game correlation pricing."
)

df, is_live = load_props_data()

col_refresh, col_status = st.columns([1, 5])
with col_refresh:
    if st.button("🔄 Refresh data"):
        load_props_data.clear()
        st.rerun()
with col_status:
    if is_live:
        st.success("Showing live lines scraped from Covers.com.", icon="✅")
    else:
        st.warning(
            "Covers.com didn't return any postable lines right now (common in the offseason/"
            "before a book posts a slate), so **sample/demo data** is shown below instead. "
            "Numbers are illustrative only -- not real, current odds. Click Refresh to try live "
            "data again.",
            icon="⚠️",
        )

if df.empty:
    st.error("No data available from either the live scraper or the sample dataset.")
    st.stop()

# ---------------------------------------------------------------------------
# Sidebar: filters
# ---------------------------------------------------------------------------
st.sidebar.header("🎯 Filters")

enforce_cap = st.sidebar.checkbox(
    "Enforce WR/RB +125 longshot cap", value=True,
    help="Drops WR/RB props priced worse than +125. QB and TE props are never capped.",
)

leagues = sorted(df["League"].dropna().unique().tolist())
positions = sorted(df["Position"].dropna().unique().tolist())
prop_types = sorted(df["PropType"].dropna().unique().tolist())
sportsbooks = sorted(df["Sportsbook"].dropna().unique().tolist())

selected_leagues = st.sidebar.multiselect("League", options=leagues, default=leagues)
selected_positions = st.sidebar.multiselect("Position", options=positions, default=positions)
selected_props = st.sidebar.multiselect("Prop Type", options=prop_types, default=prop_types)
selected_books = st.sidebar.multiselect("Sportsbook", options=sportsbooks, default=sportsbooks)
player_search = st.sidebar.text_input("Search player name")

st.sidebar.header("📊 Sorting")
sort_label_map = {
    "Edge %": "EdgePct",
    "Sportsbook Odds": "SportsbookOdds",
    "Position": "Position",
    "Game Time": "GameTime",
}
sort_label = st.sidebar.selectbox("Sort by", options=list(sort_label_map.keys()))
sort_ascending = st.sidebar.radio("Direction", options=["Descending", "Ascending"]) == "Ascending"

st.sidebar.header("🎰 Parlay Settings")
correlation_multiplier = st.sidebar.slider(
    "Same-game correlation multiplier",
    min_value=1.05, max_value=1.25, value=1.15, step=0.01,
    help=(
        "When 2+ selected legs are from the same game, their joint true "
        "probability is boosted by this factor (typically 1.12x-1.18x) to "
        "reflect positive correlation -- e.g. if the offense is scoring "
        "touchdowns, both a QB's and his TE's Anytime TD become more "
        "likely together than independent games would suggest."
    ),
)

# ---------------------------------------------------------------------------
# Apply filters
# ---------------------------------------------------------------------------
working_df = apply_longshot_filter(df, enforce_cap)
working_df = working_df[
    working_df["League"].isin(selected_leagues)
    & working_df["Position"].isin(selected_positions)
    & working_df["PropType"].isin(selected_props)
    & working_df["Sportsbook"].isin(selected_books)
]
if player_search.strip():
    working_df = working_df[
        working_df["Player"].str.contains(player_search.strip(), case=False, na=False)
    ]

working_df = working_df.sort_values(by=sort_label_map[sort_label], ascending=sort_ascending)

if "Select" not in working_df.columns:
    working_df = working_df.copy()
    working_df.insert(0, "Select", False)

# ---------------------------------------------------------------------------
# Main grid
# ---------------------------------------------------------------------------
st.subheader(f"📋 Filtered Slate ({len(working_df)} plays)")
st.write("Check the boxes on the left to add plays to your parlay ticket below.")

display_cols = [c for c in DISPLAY_COLUMNS if c in working_df.columns]
grid_df = working_df[display_cols]

edited_df = st.data_editor(
    grid_df,
    column_config={
        "Select": st.column_config.CheckboxColumn(required=True),
        "GameTime": st.column_config.DatetimeColumn("Time", format="ddd h:mma"),
        "Position": st.column_config.TextColumn("POS"),
        "CoversLine": st.column_config.NumberColumn("Line", format="%.1f"),
        "SportsbookOdds": st.column_config.NumberColumn("SB Odds", format="%+d"),
        "ModelFairOdds": st.column_config.NumberColumn("Model", format="%+d"),
        "EdgePct": st.column_config.NumberColumn("Edge %", format="%.1f%%"),
        "PropType": st.column_config.TextColumn("Prop Type"),
        "InjuryStatus": st.column_config.TextColumn("Injury Status"),
    },
    disabled=[c for c in grid_df.columns if c != "Select"],
    hide_index=True,
    width="stretch",
    key="props_grid",
)

# ---------------------------------------------------------------------------
# Parlay builder
# ---------------------------------------------------------------------------
st.divider()
st.subheader("🎰 Live Parlay Ticket")

selected_rows = edited_df[edited_df["Select"] == True].copy()  # noqa: E712

if selected_rows.empty:
    st.info("Select one or more plays above to build and price a parlay.")
else:
    # Bring back the columns needed for pricing that may not be in the
    # display grid (Opponent is used for same-game grouping).
    full_selected = working_df.loc[selected_rows.index]

    st.write(
        full_selected[
            [c for c in ["GameTime", "Player", "Team", "Matchup", "PropType",
                         "CoversLine", "SportsbookOdds", "ModelFairOdds", "EdgePct"]
            if c in full_selected.columns]
        ]
    )

    sportsbook_pricing = price_parlay(full_selected, odds_col="SportsbookOdds", correlation_multiplier=1.0)
    model_pricing = price_parlay(full_selected, odds_col="ModelFairOdds", correlation_multiplier=correlation_multiplier)

    c1, c2, c3 = st.columns(3)
    c1.metric("Sportsbook Parlay Payout", format_american(sportsbook_pricing["combined_american"]))
    c2.metric(
        "Model Fair Parlay Odds",
        format_american(model_pricing["combined_american"]),
        help="Your model's true combined probability, with same-game correlation applied.",
    )

    sportsbook_prob = sportsbook_pricing["combined_probability"] or 0
    model_prob = model_pricing["combined_probability"] or 0
    if sportsbook_prob > 0:
        parlay_edge_pct = (model_prob - sportsbook_prob) / sportsbook_prob * 100
    else:
        parlay_edge_pct = 0.0
    c3.metric("Parlay Value Edge", f"{parlay_edge_pct:+.1f}%")

    correlated_groups = [g for g in model_pricing["groups"] if g["correlated"]]
    if correlated_groups:
        st.caption(
            f"🔗 Same-game correlation applied ({correlation_multiplier:.2f}x) to "
            + "; ".join(
                f"{', '.join(g['players'])}" for g in correlated_groups
            )
        )

st.divider()
st.caption(
    "For informational and entertainment purposes only -- not betting advice. Odds and "
    "'edges' shown may be based on illustrative sample data (see banner above). Please "
    "gamble responsibly. 21+. If you or someone you know has a gambling problem, call or "
    "text the National Problem Gambling Helpline at 1-800-522-4700."
)

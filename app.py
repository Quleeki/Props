"""
EdgeFinder: NFL + CFB Player Prop Dashboard & Parlay Builder

Pulls player prop lines (live from Covers.com when available, falling back
to bundled sample data otherwise), filters out WR/RB longshots priced worse
than +125, lets you filter/sort/search the slate, and prices a live parlay
ticket -- including a same-game correlation boost -- as you check rows or
remove them directly from the ticket.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

import sample_data
import scraper
from parlay import best_book_parlay, format_american, price_parlay

st.set_page_config(page_title="EdgeFinder: Prop Dashboard & Parlay Builder", layout="wide")

DISPLAY_COLUMNS = [
    "Select",
    "GameTime",
    "League",
    "Position",
    "Player",
    "Game",
    "PropType",
    "CoversLine",
    "SportsbookOdds",
    "Sportsbook",
    "ModelFairOdds",
    "EdgePct",
    "InjuryStatus",
]


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


def _clear_player_search() -> None:
    st.session_state.player_search_input = ""


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

st.sidebar.subheader("🔎 Player search")
search_col, clear_col = st.sidebar.columns([3, 1])
with search_col:
    player_search = st.text_input(
        "Search player name",
        key="player_search_input",
        placeholder="e.g. Saquon",
        label_visibility="collapsed",
    )
with clear_col:
    st.button("✖ Clear", on_click=_clear_player_search, width="stretch")

st.sidebar.header("📊 Sorting")
sort_label_map = {
    "Edge %": "EdgePct",
    "Sportsbook Odds": "SportsbookOdds",
    "Position": "Position",
    "Game Time": "GameTime",
    "Game": "Game",
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
# capped_df keeps the WR/RB +125 rule (a data-integrity rule) but NOT the
# view-only sidebar filters below -- it's the search space for "which
# sportsbook has every selected leg", so an unrelated league/position/book
# filter toggle doesn't hide a book that actually has your full parlay. Its
# row index also doubles as the stable id used to track parlay selections
# below, independent of whatever filter/sort/search view is on screen.
capped_df = apply_longshot_filter(df, enforce_cap)
working_df = capped_df[
    capped_df["League"].isin(selected_leagues)
    & capped_df["Position"].isin(selected_positions)
    & capped_df["PropType"].isin(selected_props)
    & capped_df["Sportsbook"].isin(selected_books)
]
if player_search.strip():
    working_df = working_df[
        working_df["Player"].str.contains(player_search.strip(), case=False, na=False)
    ]

working_df = working_df.sort_values(by=sort_label_map[sort_label], ascending=sort_ascending)

# ---------------------------------------------------------------------------
# Parlay selection state
# ---------------------------------------------------------------------------
# Selected legs are tracked by row id (independent of whatever filter/sort/
# search view is currently on screen), so a leg stays on the ticket -- and
# can be removed from the ticket directly, with no need to scroll back up
# and find its checkbox -- even after you filter it out of view.
if "parlay_leg_ids" not in st.session_state:
    st.session_state.parlay_leg_ids = set()

# Drop any stale ids that no longer exist in the current data (e.g. right
# after a manual data refresh reshuffles the underlying rows).
st.session_state.parlay_leg_ids &= set(capped_df.index)

# ---------------------------------------------------------------------------
# Main grid
# ---------------------------------------------------------------------------
st.subheader(f"📋 Filtered Slate ({len(working_df)} plays)")
st.write("Check the boxes on the left to add plays to your parlay ticket below.")

non_select_cols = [c for c in DISPLAY_COLUMNS if c != "Select" and c in working_df.columns]
grid_df = working_df[non_select_cols].copy()
grid_df.insert(0, "Select", grid_df.index.isin(st.session_state.parlay_leg_ids))

edited_df = st.data_editor(
    grid_df,
    column_config={
        "Select": st.column_config.CheckboxColumn(required=True),
        "GameTime": st.column_config.DatetimeColumn("Time", format="ddd h:mma"),
        "Position": st.column_config.TextColumn("POS"),
        "Game": st.column_config.TextColumn("Game"),
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

# Reconcile checkbox edits made in the currently-visible rows back into the
# persistent selection set. Rows not currently on screen (hidden by a
# filter/search) are left untouched, so they stay on the parlay ticket.
visible_ids = edited_df.index
edited_selected = set(visible_ids[edited_df["Select"] == True])  # noqa: E712
edited_unselected = set(visible_ids) - edited_selected
st.session_state.parlay_leg_ids |= edited_selected
st.session_state.parlay_leg_ids -= edited_unselected

# ---------------------------------------------------------------------------
# Parlay builder
# ---------------------------------------------------------------------------
st.divider()
st.subheader("🎰 Live Parlay Ticket")

full_selected = capped_df.loc[capped_df.index.isin(st.session_state.parlay_leg_ids)]
if "GameTime" in full_selected.columns and not full_selected.empty:
    full_selected = full_selected.sort_values("GameTime")

if full_selected.empty:
    st.info("Select one or more plays above to build and price a parlay.")
else:
    st.caption(f"{len(full_selected)} leg(s) selected -- click 🗑️ to remove one without scrolling back up.")

    leg_col_widths = [2.4, 1.8, 2.0, 1.1, 1.3, 1.6, 0.7]
    header_cells = st.columns(leg_col_widths)
    for label, cell in zip(["Player", "Game", "Prop Type", "Line", "Odds", "Sportsbook", ""], header_cells):
        cell.markdown(f"**{label}**")

    for leg_id, leg in full_selected.iterrows():
        row_cells = st.columns(leg_col_widths)
        row_cells[0].write(leg.get("Player", ""))
        row_cells[1].write(leg.get("Game", ""))
        row_cells[2].write(leg.get("PropType", ""))
        row_cells[3].write(leg.get("CoversLine", ""))
        odds_val = leg.get("SportsbookOdds")
        row_cells[4].write(format_american(int(odds_val)) if pd.notna(odds_val) else "")
        row_cells[5].write(leg.get("Sportsbook", ""))
        if row_cells[6].button("🗑️", key=f"remove_leg_{leg_id}", help="Remove this leg from the parlay"):
            st.session_state.parlay_leg_ids.discard(leg_id)
            if "props_grid" in st.session_state:
                del st.session_state["props_grid"]
            st.rerun()

    st.divider()

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

    # ---- Which single sportsbook has the best price for THIS exact combo ----
    best = best_book_parlay(capped_df, full_selected, odds_col="SportsbookOdds")
    if best["has_complete_book"]:
        winner = best["best"]
        st.success(
            f"🏦 Best single-book parlay: **{winner['sportsbook']}** at "
            f"{format_american(winner['combined_american'])} -- all {len(full_selected)} "
            "selected legs are available there.",
            icon="🏆",
        )
        if len(best["all_books"]) > 1:
            with st.expander("Compare every sportsbook that carries all selected legs"):
                st.dataframe(
                    pd.DataFrame(
                        [
                            {"Sportsbook": r["sportsbook"], "Combined Odds": format_american(r["combined_american"])}
                            for r in best["all_books"]
                        ]
                    ),
                    hide_index=True,
                    width="stretch",
                )
    else:
        st.info(
            "No single sportsbook currently prices every selected leg -- the 'Sportsbook "
            "Parlay Payout' above mixes each leg's own book. Try a combination where all "
            "legs share at least one common sportsbook to see a same-book comparison.",
            icon="ℹ️",
        )

st.divider()
st.caption(
    "For informational and entertainment purposes only -- not betting advice. Odds and "
    "'edges' shown may be based on illustrative sample data (see banner above). Please "
    "gamble responsibly. 21+. If you or someone you know has a gambling problem, call or "
    "text the National Problem Gambling Helpline at 1-800-522-4700."
)

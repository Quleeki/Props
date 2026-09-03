"""
EdgeFinder: NFL + CFB Player Prop Dashboard & Parlay Builder

Pulls player prop lines -- preferring real sportsbook lines live from
Covers.com, falling back to Covers' prediction-market pricing (Kalshi/
Novig/etc) per-prop when your preferred books aren't available, and only
falling all the way back to bundled sample data if Covers has nothing
postable at all -- filters out WR/RB longshots priced worse than +125, lets
you filter/sort/search the slate, and prices a live parlay ticket --
including a same-game correlation boost -- as you check rows or remove them
directly from the ticket. "Edge %" comes from model.py's fair-odds
estimate, not the sportsbook's own number.
"""

from __future__ import annotations

import sys

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
    "Source",
    "ModelFairOdds",
    "EdgePct",
    "InjuryStatus",
]

# Friendly labels for the DataSource tag scraper.py/sample_data.py attach to
# every row, shown as the "Source" column so it's obvious at a glance which
# rows are a real bettable sportsbook line vs. prediction-market pricing
# (see scraper.py's module docstring) vs. illustrative demo data.
_SOURCE_LABELS = {
    "COVERS": "Sportsbook",
    "COVERS_PREDICTION_MARKET": "Prediction Mkt",
    "SAMPLE": "Sample",
}


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------
@st.cache_data(ttl=900, show_spinner="Fetching the latest props...")
def load_props_data() -> tuple[pd.DataFrame, str]:
    """Returns (dataframe, data_source). data_source describes the WORST
    (least-preferred) source actually present in the returned data:

      "COVERS" -- every row is a real sportsbook line from Covers.com.
      "COVERS_PREDICTION_MARKET" -- at least one row (not necessarily all --
          see the per-row "Source" column in the grid) came from Covers'
          prediction-market fallback rather than a preferred sportsbook.
      "SAMPLE" -- Covers returned nothing postable at all; bundled demo data.

    Tries the Covers.com scraper first (which itself prefers real
    sportsbook pricing per-prop and only falls back to prediction-market
    pricing for props none of your books priced -- see scraper.py), and
    only falls back to sample data if that returns nothing at all."""
    try:
        live_df = scraper.fetch_all_props()
    except Exception:
        # Print the full traceback so it shows up in Streamlit Cloud's
        # "Manage app" logs -- otherwise a real bug here fails silently
        # and just looks like "no live props available".
        import traceback

        print("[app] fetch_all_props() raised an exception:", file=sys.stderr)
        traceback.print_exc()
        live_df = pd.DataFrame()

    if live_df is not None and not live_df.empty:
        sources = set(live_df["DataSource"].unique()) if "DataSource" in live_df.columns else set()
        if "COVERS_PREDICTION_MARKET" in sources:
            print(
                f"[app] loaded {len(live_df)} live row(s) from Covers.com "
                "(some from prediction-market fallback).",
                file=sys.stderr,
            )
            return live_df, "COVERS_PREDICTION_MARKET"
        print(f"[app] loaded {len(live_df)} live row(s) from Covers.com.", file=sys.stderr)
        return live_df, "COVERS"

    print("[app] no live rows -- falling back to sample data.", file=sys.stderr)
    return sample_data.get_sample_data(), "SAMPLE"


def apply_longshot_filter(df: pd.DataFrame, enforce: bool) -> pd.DataFrame:
    """Enforces that WR/RB props priced above +125 are dropped, while QB/TE
    pricing stays unrestricted -- the core filtering rule from the spec."""
    if not enforce or df.empty:
        return df
    is_longshot_skill_position = df["Position"].isin(["WR", "RB"]) & (df["SportsbookOdds"] > 125)
    return df[~is_longshot_skill_position].reset_index(drop=True)


def best_line_per_prop(df: pd.DataFrame) -> pd.DataFrame:
    """Collapse multiple sportsbooks' rows for the same prop down to just
    the single most favorable line for the bettor -- e.g. if DraftKings has
    a QB's Passing Yards Over at 218.5 and Bet365 has it at 225.5, only the
    DraftKings row (the easier Over) is shown in the slate. 'Best' means
    the lowest line for an Over and the highest line for an Under; props
    with no explicit Over/Under side in their name (sample data, and the
    Anytime TD moneyline convention) instead keep whichever row has the
    better sportsbook odds. Ties are broken the same way.

    This only changes what's displayed -- the full multi-book data (what
    this function is called on) is still what backs the "best single book
    for your whole parlay" comparison down in the parlay ticket, since that
    logic works off `capped_df` directly, not this collapsed view."""
    if df.empty:
        return df
    group_cols = [c for c in ("Player", "PropType", "Game", "GameTime") if c in df.columns]
    if not group_cols or "CoversLine" not in df.columns or "SportsbookOdds" not in df.columns:
        return df

    prop_type_pos = group_cols.index("PropType") if "PropType" in group_cols else None

    def _pick_best_index(group: pd.DataFrame):
        # NOTE: pandas excludes the groupby columns themselves from `group`
        # here, so PropType (one of group_cols) isn't a column on `group` --
        # read it from the group key (`group.name`) instead.
        if prop_type_pos is None:
            prop_type = ""
        elif isinstance(group.name, tuple):
            prop_type = str(group.name[prop_type_pos]).lower()
        else:
            prop_type = str(group.name).lower()

        if "(under)" in prop_type:
            best_line = group["CoversLine"].max()
        elif "(over)" in prop_type:
            best_line = group["CoversLine"].min()
        else:
            best_line = None  # no explicit side -- fall through to odds-only tie-break

        candidates = group[group["CoversLine"] == best_line] if best_line is not None else group
        return candidates["SportsbookOdds"].idxmax()

    best_indices = df.groupby(group_cols, dropna=False, sort=False).apply(_pick_best_index)
    return df.loc[best_indices.values]


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

df, data_source = load_props_data()

col_refresh, col_status = st.columns([1, 5])
with col_refresh:
    if st.button("🔄 Refresh data"):
        load_props_data.clear()
        st.rerun()
with col_status:
    if data_source == "COVERS":
        st.success("Showing live lines scraped from Covers.com.", icon="✅")
    elif data_source == "COVERS_PREDICTION_MARKET":
        n_pred = int((df["DataSource"] == "COVERS_PREDICTION_MARKET").sum()) if "DataSource" in df.columns else 0
        st.info(
            f"Showing live Covers.com lines -- {n_pred} of {len(df)} row(s) came from "
            "prediction-market pricing (Kalshi/Novig/Polymarket/etc, see the **Source** column) "
            "instead of your preferred sportsbooks, likely because this scrape's proxy IP didn't "
            "land in a state carrying DraftKings/BetMGM/Bet365/theScore Bet. These are still "
            "real, live market prices -- just not from a traditional sportsbook. Click Refresh "
            "to try again.",
            icon="🔀",
        )
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

# Collapse each prop down to just its single best line for display (see
# best_line_per_prop docstring) -- e.g. don't show both "218.5 DraftKings"
# and "225.5 Bet365" rows for the same QB's Passing Yards Over; just the
# easier 218.5 one. The full multi-book working_df/capped_df is untouched
# and still what the parlay ticket's cross-book matching uses.
display_df = best_line_per_prop(working_df)
if "DataSource" in display_df.columns:
    display_df = display_df.copy()
    display_df["Source"] = display_df["DataSource"].map(_SOURCE_LABELS).fillna(display_df["DataSource"])

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
st.subheader(f"📋 Filtered Slate ({len(display_df)} plays)")
st.write("Check the boxes on the left to add plays to your parlay ticket below.")
st.caption(
    "Showing each prop's single best line across your selected sportsbooks. "
    "Add a leg to the parlay ticket below to see which other books carry it too."
)

non_select_cols = [c for c in DISPLAY_COLUMNS if c != "Select" and c in display_df.columns]
grid_df = display_df[non_select_cols].copy()
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
        "Source": st.column_config.TextColumn("Source", help="Sportsbook = real book odds. Prediction Mkt = Covers' fallback pricing when none of your preferred books had this prop (see banner above). Sample = demo data."),
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

"""
scraper.py

Live player-prop scraper for:
    NFL  -> https://www.covers.com/sport/football/nfl/player-props
    NCAAF/CFB -> https://www.covers.com/sport/football/ncaaf/player-props

IMPORTANT - please read before relying on this in production:

This sandbox environment has no outbound internet access to covers.com, so
this module was written defensively from general knowledge of how sites
like this are typically built (Next.js apps that embed a JSON payload in a
`<script id="__NEXT_DATA__">` tag, with an HTML table as a progressive-
enhancement fallback) rather than against the live DOM. It has NOT been
verified against the real page.

Before you rely on it:
  1. Run `python scraper.py --debug` on a machine with real internet access.
     This saves the raw HTML for each league to ./debug_html/ and prints
     how many rows each parsing strategy found.
  2. Open debug_html/nfl.html in a browser or text editor, search for a
     player name you know is currently listed, and see which parsing
     strategy (JSON payload vs. HTML table) actually contains the data.
  3. Adjust JSON_KEY_ALIASES / the table-parsing selectors below to match
     what you find. This is the one part of the project that will need
     hands-on iteration once real data is live on the page, since site
     markup can't be reverse-engineered without fetching it.

The app (app.py) calls fetch_all_props() and gracefully falls back to
sample_data.py if this returns no rows, so the dashboard is always usable
even before the scraper is fully dialed in.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

import pandas as pd
import requests
from bs4 import BeautifulSoup

COVERS_URLS = {
    "NFL": "https://www.covers.com/sport/football/nfl/player-props",
    "CFB": "https://www.covers.com/sport/football/ncaaf/player-props",
}

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

REQUEST_TIMEOUT = 20

# If set, route the request through ScrapingBee for JS rendering / anti-bot
# handling instead of a plain requests.get(). Cheaper than Apify per the
# original design discussion; only kicks in if the key is present.
SCRAPINGBEE_API_KEY = os.environ.get("SCRAPINGBEE_API_KEY", "").strip()
SCRAPINGBEE_ENDPOINT = "https://app.scrapingbee.com/api/v1/"

# Best-effort key aliases for mapping an unknown JSON payload's field names
# onto our schema. Extend this once you've inspected a real __NEXT_DATA__
# (or similar) blob from the live page.
JSON_KEY_ALIASES = {
    "player": ["player", "playerName", "player_name", "name", "athlete"],
    "team": ["team", "teamAbbr", "team_abbreviation", "teamCode"],
    "opponent": ["opponent", "opp", "opponentAbbr", "vsTeam"],
    "position": ["position", "pos"],
    "prop_type": ["propType", "prop_type", "market", "statType", "category"],
    "line": ["line", "handicap", "propLine", "value"],
    "odds": ["odds", "price", "americanOdds", "overOdds", "over_odds"],
    "game_time": ["gameTime", "game_time", "startTime", "kickoff", "eventDate"],
    "sportsbook": ["sportsbook", "book", "bookName"],
}


def _get_html(url: str) -> str | None:
    """Fetch raw HTML, optionally proxied through ScrapingBee for JS
    rendering. Returns None (never raises) on any failure so callers can
    fall back cleanly."""
    try:
        if SCRAPINGBEE_API_KEY:
            resp = requests.get(
                SCRAPINGBEE_ENDPOINT,
                params={
                    "api_key": SCRAPINGBEE_API_KEY,
                    "url": url,
                    "render_js": "true",
                },
                timeout=REQUEST_TIMEOUT,
            )
        else:
            resp = requests.get(url, headers=REQUEST_HEADERS, timeout=REQUEST_TIMEOUT)

        if resp.status_code != 200:
            print(f"[scraper] {url} -> HTTP {resp.status_code}", file=sys.stderr)
            return None
        return resp.text
    except requests.RequestException as exc:
        print(f"[scraper] request failed for {url}: {exc}", file=sys.stderr)
        return None


def _first_present(d: dict, keys: list[str]):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


def _looks_like_prop_row(d: dict) -> bool:
    """Heuristic: a dict is 'prop-row-shaped' if it has something that
    looks like a player name AND something that looks like odds/a line."""
    if not isinstance(d, dict):
        return False
    has_player = _first_present(d, JSON_KEY_ALIASES["player"]) is not None
    has_odds = (
        _first_present(d, JSON_KEY_ALIASES["odds"]) is not None
        or _first_present(d, JSON_KEY_ALIASES["line"]) is not None
    )
    return has_player and has_odds


def _walk_json_for_prop_rows(obj, found: list) -> None:
    """Recursively search an arbitrary JSON structure for dicts that look
    like individual prop rows."""
    if isinstance(obj, dict):
        if _looks_like_prop_row(obj):
            found.append(obj)
        for v in obj.values():
            _walk_json_for_prop_rows(v, found)
    elif isinstance(obj, list):
        for item in obj:
            _walk_json_for_prop_rows(item, found)


def _parse_next_data(html: str) -> list[dict]:
    """Try to extract structured prop rows from a Next.js __NEXT_DATA__ (or
    similarly embedded) JSON blob."""
    soup = BeautifulSoup(html, "lxml")

    candidates = []
    next_data_tag = soup.find("script", id="__NEXT_DATA__")
    if next_data_tag and next_data_tag.string:
        candidates.append(next_data_tag.string)

    # Also catch any other application/json script tags as a fallback net.
    for tag in soup.find_all("script", type="application/json"):
        if tag.string:
            candidates.append(tag.string)

    rows: list[dict] = []
    for raw in candidates:
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        _walk_json_for_prop_rows(payload, rows)

    return rows


def _parse_html_tables(html: str) -> list[dict]:
    """Fallback: parse any <table> rows on the page directly. This is
    intentionally generic since we haven't seen the live markup -- it
    extracts cell text and uses regex to spot odds-shaped tokens
    (e.g. +145, -110) rather than relying on specific CSS classes."""
    soup = BeautifulSoup(html, "lxml")
    odds_pattern = re.compile(r"^[+-]\d{2,4}$")
    rows: list[dict] = []

    for table in soup.find_all("table"):
        headers = [th.get_text(strip=True) for th in table.find_all("th")]
        for tr in table.find_all("tr"):
            cells = [td.get_text(strip=True) for td in tr.find_all("td")]
            if not cells:
                continue
            odds_cells = [c for c in cells if odds_pattern.match(c)]
            if not odds_cells:
                continue  # not a data row (probably a header or spacer row)

            row = {"_raw_cells": cells, "_headers": headers, "_odds_found": odds_cells}
            rows.append(row)

    return rows


def _normalize_row(raw: dict, league: str) -> dict | None:
    """Map a best-effort-extracted raw dict onto our standard schema.
    Returns None if we can't find the minimum required fields."""
    player = _first_present(raw, JSON_KEY_ALIASES["player"])
    odds = _first_present(raw, JSON_KEY_ALIASES["odds"])
    if not player or odds is None:
        return None

    try:
        odds = int(float(odds))
    except (TypeError, ValueError):
        return None

    team = _first_present(raw, JSON_KEY_ALIASES["team"]) or ""
    opponent = _first_present(raw, JSON_KEY_ALIASES["opponent"]) or ""

    return {
        "League": league,
        "Position": _first_present(raw, JSON_KEY_ALIASES["position"]) or "",
        "Player": player,
        "Team": team,
        "Opponent": opponent,
        "Matchup": f"vs {opponent}" if opponent else "",
        "GameTime": _first_present(raw, JSON_KEY_ALIASES["game_time"]) or "",
        "PropType": _first_present(raw, JSON_KEY_ALIASES["prop_type"]) or "",
        "CoversLine": _first_present(raw, JSON_KEY_ALIASES["line"]),
        "SportsbookOdds": odds,
        "Sportsbook": _first_present(raw, JSON_KEY_ALIASES["sportsbook"]) or "Covers",
        "ModelFairOdds": odds,  # no independent model signal from the scrape itself
        "EdgePct": 0.0,
        "InjuryStatus": "",
        "DataSource": "COVERS",
    }


def fetch_covers_props(league: str, save_debug_html: bool = False) -> list[dict]:
    """Fetch + parse player props for one league ('NFL' or 'CFB').
    Always returns a list (possibly empty) and never raises."""
    url = COVERS_URLS.get(league)
    if not url:
        raise ValueError(f"Unknown league '{league}'. Expected one of {list(COVERS_URLS)}.")

    html = _get_html(url)
    if not html:
        return []

    if save_debug_html:
        debug_dir = Path("debug_html")
        debug_dir.mkdir(exist_ok=True)
        (debug_dir / f"{league.lower()}.html").write_text(html, encoding="utf-8")

    json_rows = _parse_next_data(html)
    normalized = [r for r in (_normalize_row(r, league) for r in json_rows) if r]
    if normalized:
        return normalized

    # Fall back to raw table extraction. These rows are NOT normalized to
    # the final schema (we don't know the column order yet) -- they're
    # surfaced so you can inspect them with --debug and wire up real
    # column mapping once you see the structure.
    table_rows = _parse_html_tables(html)
    if table_rows:
        print(
            f"[scraper] {league}: found {len(table_rows)} raw table row(s) but no "
            "JSON payload -- table parsing needs column mapping, see --debug output.",
            file=sys.stderr,
        )
    return []


def fetch_all_props(save_debug_html: bool = False) -> pd.DataFrame:
    """Fetch NFL + CFB props and return one combined DataFrame matching the
    schema used throughout the app. Returns an empty DataFrame (not None)
    if nothing could be scraped, so callers can fall back to sample data."""
    all_rows: list[dict] = []
    for league in COVERS_URLS:
        rows = fetch_covers_props(league, save_debug_html=save_debug_html)
        all_rows.extend(rows)

    df = pd.DataFrame(all_rows)
    if not df.empty and "GameTime" in df.columns:
        df["GameTime"] = pd.to_datetime(df["GameTime"], errors="coerce")
    return df


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Debug the Covers.com props scraper.")
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Save raw HTML for each league to ./debug_html/ and print row counts.",
    )
    args = parser.parse_args()

    result = fetch_all_props(save_debug_html=args.debug)
    print(f"Fetched {len(result)} total prop row(s) across {list(COVERS_URLS)}.")
    if not result.empty:
        print(result.head(10).to_string(index=False))
    else:
        print(
            "No rows parsed. If --debug was passed, inspect ./debug_html/*.html "
            "and update JSON_KEY_ALIASES / _parse_html_tables in scraper.py to match "
            "the real markup."
        )

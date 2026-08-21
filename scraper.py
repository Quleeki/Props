"""
scraper.py

Live player-prop scraper for:
    NFL  -> https://www.covers.com/sport/football/nfl/player-props
    NCAAF/CFB -> https://www.covers.com/sport/football/ncaaf/player-props

Covers.com renders its prop cards server-side (no hidden API call needed)
using a card-based layout, NOT html <table> tags and NOT a Next.js
__NEXT_DATA__ blob. Each prop card looks roughly like:

    <div class="category-title ...">
        <a class="player-link" href="/sport/.../players/194249/miguel-vargas">M. Vargas</a>
        <span class="player-position"> (3B)</span>
        <span class="prediction ...">0.5 Total RBIs</span>
    </div>
    ...
    <div class="best-odd-container ...">
        <a class="deeplink" data-tracking='{"text":"ATL vs CHW, Thu, Aug 20 • 2:10 PM ET",
                                             "elementText":"o0.5 +171 draftkings"}' ...>
            <b>o0.5</b> +171
        </a>
        <button data-bs-target="#120636756-proj-odds" ...>...</button>
    </div>
    <div id="120636756-proj-odds" class="tab-pane ... compare-odds-table">
        <div class="compare-odds-column">
            <img class="sportsbook-logo" alt="BetMGM logo" ...>
            <a class="book-odds" data-tracking='{"elementText":"o0.5 +160 + betmgm", ...}'>...</a>
        </div>
        ... (one .compare-odds-column per additional sportsbook)
    </div>

_parse_prop_cards() below targets exactly this structure -- confirmed
against a real saved copy of the MLB player-props page (NFL/NCAAF render
the same way, they just have no props posted yet for the upcoming slate).
The matchup + kickoff time and the over/under + line + odds + sportsbook
for every book comparison are all recovered from the `data-tracking`
JSON attribute on each odds link, which is far more reliable than trying
to parse the visible text/CSS layout directly.

A couple of older, more speculative parsing strategies (_parse_next_data /
_parse_html_tables) are kept as fallbacks in case Covers changes their
markup again, but _parse_prop_cards is the primary, verified path.

Run `python scraper.py --debug` to save the raw HTML for each league to
./debug_html/ and see row counts from each parsing strategy -- handy if
Covers tweaks their markup again in the future.

The app (app.py) calls fetch_all_props() and gracefully falls back to
sample_data.py if this returns no rows (e.g. NFL/NCAAF simply have no
props posted yet), so the dashboard is always usable either way.
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
from dateutil import parser as dateutil_parser

COVERS_URLS = {
    "NFL": "https://www.covers.com/sport/football/nfl/player-props",
    "CFB": "https://www.covers.com/sport/football/ncaaf/player-props",
}

# Only these sportsbooks' odds are kept from live scrapes -- everything else
# Covers lists (FanDuel, Caesars, etc.) is silently dropped. Match against
# the lowercased, space-stripped slug Covers uses in its data-tracking
# attributes (see _format_sportsbook_name below for the slug -> display-name
# mapping). Edit this set to change which books show up in the app.
PREFERRED_SPORTSBOOKS = {"draftkings", "betmgm", "bet365", "thescore", "thescorebet"}

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


# Matches data-tracking "elementText" values like "o0.5 +171 draftkings"
# (best-odds block) or "o0.5 +160 + betmgm" (compare-odds block, which adds
# a stray "+" separator before the sportsbook slug -- the optional \+?\s*
# absorbs that): (o|u) side, line, American odds, sportsbook slug.
_ELEMENT_TEXT_RE = re.compile(r"^([ou])\s*([\d.]+)\s*([+\-]\d+)\s+\+?\s*(.+)$", re.IGNORECASE)

# Matches data-tracking "text" values like
# "ATL vs CHW, Thu, Aug 20 • 2:10 PM ET" -> team, opponent, date/time.
_MATCHUP_TEXT_RE = re.compile(r"^(\S+)\s+vs\s+(\S+),\s*(.+)$")

# Friendlier display names for known sportsbook slugs; anything not listed
# here falls back to .title() (e.g. "unknownbook" -> "Unknownbook").
_SPORTSBOOK_NAME_MAP = {
    "draftkings": "DraftKings",
    "fanduel": "FanDuel",
    "betmgm": "BetMGM",
    "caesars": "Caesars",
    "bet365": "Bet365",
    "betrivers": "BetRivers",
    "espnbet": "ESPN BET",
    "fanatics": "Fanatics",
    "wynnbet": "WynnBET",
    "pointsbet": "PointsBet",
    "thescore": "theScore Bet",
    "thescorebet": "theScore Bet",
}


def _format_sportsbook_name(slug: str) -> str:
    key = slug.strip().lower()
    return _SPORTSBOOK_NAME_MAP.get(key, slug.strip().title())


def _parse_prop_cards(html: str, league: str) -> list[dict]:
    """Primary parser: extract prop rows from Covers' server-rendered
    div-based prop cards (see module docstring for the confirmed markup
    shape). Returns one row per (player, prop, sportsbook) combination."""
    soup = BeautifulSoup(html, "lxml")
    rows: list[dict] = []

    for category_title in soup.find_all("div", class_="category-title"):
        player_link = category_title.find("a", class_="player-link")
        if not player_link:
            continue
        player_name = player_link.get_text(strip=True)

        position_tag = category_title.find("span", class_="player-position")
        position = position_tag.get_text(strip=True).strip("() ") if position_tag else ""

        prediction_tag = category_title.find("span", class_="prediction")
        prediction_text = prediction_tag.get_text(strip=True) if prediction_tag else ""
        line_match = re.match(r"^([\d.]+)\s+(.*)$", prediction_text)
        if line_match:
            fallback_line = float(line_match.group(1))
            prop_type = line_match.group(2).strip()
        else:
            fallback_line = None
            prop_type = prediction_text

        # Walk up from the category-title looking for the ancestor "card"
        # div that also contains a .best-odd-container -- that's our
        # anchor for finding this specific player's odds links.
        card = None
        node = category_title.find_parent("div")
        for _ in range(5):
            if node is None:
                break
            if node.find("div", class_="best-odd-container"):
                card = node
                break
            node = node.find_parent("div")

        odds_links = []
        if card is not None:
            best_odds = card.find("div", class_="best-odd-container")
            if best_odds:
                odds_links.extend(best_odds.find_all("a", attrs={"data-tracking": True}))

            # The full odds-comparison table lives OUTSIDE the card as a
            # sibling, linked only by a shared id (e.g. "#120636756-proj-
            # odds") referenced from an expand button's data-bs-target --
            # so look it up globally by id rather than assuming nesting.
            expand_btn = card.find("button", attrs={"data-bs-target": True})
            if expand_btn:
                target = expand_btn.get("data-bs-target", "")
                if target.startswith("#"):
                    compare_div = soup.find(id=target[1:])
                    if compare_div:
                        odds_links.extend(
                            compare_div.find_all("a", attrs={"data-tracking": True})
                        )

        matchup_text = ""
        book_rows = []
        seen = set()
        for link in odds_links:
            tracking_raw = link.get("data-tracking", "")
            try:
                tracking = json.loads(tracking_raw)
            except (json.JSONDecodeError, TypeError):
                continue

            element_text = tracking.get("elementText", "")
            m = _ELEMENT_TEXT_RE.match(element_text)
            if not m:
                continue
            side, line_str, odds_str, book_slug = m.groups()
            try:
                odds_val = int(odds_str)
            except ValueError:
                continue

            book_key = re.sub(r"[^a-z0-9]", "", book_slug.strip().lower())
            if book_key not in PREFERRED_SPORTSBOOKS:
                continue  # not one of your preferred books -- skip it

            if not matchup_text and tracking.get("text"):
                matchup_text = tracking["text"]

            dedupe_key = (book_slug.strip().lower(), side.lower(), odds_val)
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)

            book_rows.append(
                {
                    "side": "Over" if side.lower() == "o" else "Under",
                    "line": float(line_str),
                    "odds": odds_val,
                    "sportsbook": _format_sportsbook_name(book_slug),
                }
            )

        if not book_rows:
            continue  # couldn't recover any priced odds for this card

        team = opponent = game_time_raw = ""
        game_time_iso = None
        mm = _MATCHUP_TEXT_RE.match(matchup_text) if matchup_text else None
        if mm:
            team, opponent, game_time_raw = mm.groups()
            try:
                game_time_iso = dateutil_parser.parse(game_time_raw, fuzzy=True)
            except (ValueError, OverflowError, TypeError):
                game_time_iso = None

        # Covers' own "ATL vs CHW" phrasing lists the away team first, so we
        # carry that straight through as "Away @ Home".
        game = f"{team} @ {opponent}" if team and opponent else ""

        for book in book_rows:
            rows.append(
                {
                    "League": league,
                    "Position": position,
                    "Player": player_name,
                    "Team": team,
                    "Opponent": opponent,
                    "Game": game,
                    "GameTime": game_time_iso.isoformat() if game_time_iso else game_time_raw,
                    "PropType": f"{prop_type} ({book['side']})" if prop_type else book["side"],
                    "CoversLine": book["line"] if book["line"] is not None else fallback_line,
                    "SportsbookOdds": book["odds"],
                    "Sportsbook": book["sportsbook"],
                    "ModelFairOdds": book["odds"],
                    "EdgePct": 0.0,
                    "InjuryStatus": "",
                    "DataSource": "COVERS",
                }
            )

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
        "Game": f"{team} @ {opponent}" if team and opponent else "",
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

    # Primary strategy: Covers' real server-rendered div/card markup.
    card_rows = _parse_prop_cards(html, league)
    if card_rows:
        return card_rows

    # Fallback 1: Next.js-style embedded JSON, in case a page uses that
    # pattern instead (kept for resilience against future markup changes).
    json_rows = _parse_next_data(html)
    normalized = [r for r in (_normalize_row(r, league) for r in json_rows) if r]
    if normalized:
        return normalized

    # Fallback 2: raw <table> extraction (unlikely to hit on this site, but
    # cheap to keep around). These rows are NOT normalized to the final
    # schema -- they're surfaced so you can inspect them with --debug.
    table_rows = _parse_html_tables(html)
    if table_rows:
        print(
            f"[scraper] {league}: found {len(table_rows)} raw table row(s) but no "
            "card/JSON data -- table parsing needs column mapping, see --debug output.",
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
            "No rows parsed -- most likely Covers just hasn't posted props for these "
            "leagues yet (common before a season's Week 1). If --debug was passed and "
            "you're sure props ARE live on the site right now, inspect ./debug_html/*.html "
            "for a '.category-title' div and compare it against the markup shape documented "
            "at the top of scraper.py; Covers may have changed their layout again."
        )

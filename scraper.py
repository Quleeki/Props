"""
scraper.py

Live player-prop scraper for NFL and NCAAF/CFB.

The main league pages --
    NFL  -> https://www.covers.com/sport/football/nfl/player-props
    NCAAF/CFB -> https://www.covers.com/sport/football/ncaaf/player-props
-- are fetched once per league, but ONLY to harvest this week's game-id
list and detected country/region out of a `<div id="market-filters">`
element on that page (see _extract_market_filters and the
CATEGORY_MARKET_KEYS comment block below). Those main pages only ever
render a curated ~50-pick "All Markets" feed, not the full market, so the
actual prop data for the 4 markets this dashboard cares about (Anytime TD,
Receiving Yards, Receptions, Passing TDs) comes from one additional
request per category, per league, to Covers' filtered-league-projections
endpoint -- see fetch_covers_props() for the full fetch flow.

Covers.com renders its prop cards server-side (no hidden API call needed)
on both the main pages and the per-category endpoint responses. Each prop
is a `<section class="picks-card ...">` shaped like this (confirmed
against real, live NFL and NCAAF markup -- Sept 2026 Week 2):

    <section class="picks-card ..." data-id="136889837" ...>
        <div class="d-flex justify-content-between ...">
            <span class="_badge ...">RECEIVING YARDS</span>
            <a class="projection-game-link ...">NO @ DET</a>
        </div>
        <div class="... category-title ...">
            <a class="player-link" href="/sport/.../players/53229/chris-olave">C. Olave</a>
            <span class="player-position"> (WR)</span>
            <span class="prediction ...">u78.5 Receiving Yards</span>
        </div>
        ...
        <div class="best-odd-container ...">
            <a class="deeplink" href="/go/b?...">
                <span><b>u78.5</b>&nbsp;-110</span>
                <span><img alt="bet365" ...></span>
            </a>
            <button data-bs-target="#136889837-proj-odds"
                    data-tracking='{"text":"NO vs DET, Sun, Sep 13 . 1:00 PM ET", ...}'>...</button>
        </div>
        <div id="136889837-proj-odds" class="tab-pane ... compare-odds-table">
            <div class="compare-odds-content">
                <div class="compare-odds-column">
                    <img class="sportsbook-logo" alt="BetMGM logo" ...>
                    <a class="book-odds"><b>u78.5</b>&nbsp;-110</a>   (or just "-" if that
                </div>                                                book has no price)
                ... (one .compare-odds-column per sportsbook Covers tracks)
            </div>
        </div>
    </section>

IMPORTANT, and the reason this scraper went dark for a while: an earlier
version of this file was built against Covers' *MLB* player-props page,
where each odds link carried a `data-tracking` JSON attribute with the
side/line/odds/sportsbook baked in as a JSON string. Covers' *current* NFL
markup does NOT put that on the odds links at all -- the price is plain
visible text (`<b>u78.5</b>&nbsp;-110`) and the sportsbook name comes from
an `<img alt="...">` instead. `_parse_prop_cards()` below targets this
current, confirmed structure directly (no JSON parsing of odds needed).
The one JSON blob that *does* still exist -- on the odds-comparison
"expand" button -- is only used to recover the game's kickoff date/time.

A couple of older, more speculative parsing strategies (_parse_next_data /
_parse_html_tables) are kept as fallbacks in case Covers changes their
markup again, but _parse_prop_cards is the primary, verified path.

Run `python scraper.py --debug` to save the raw HTML for each league to
./debug_html/ and see row counts from each parsing strategy -- handy if
Covers tweaks their markup again in the future.

The app (app.py) calls fetch_all_props() and gracefully falls back to
sample_data.py if this returns no rows (e.g. NFL/NCAAF simply have no
props posted yet), so the dashboard is always usable either way.

DATA SOURCE FALLBACK (per card, not per page): Covers decides which
sportsbooks to render based on the *visitor's* US location -- a data-center
IP (or a ScrapingBee proxy that lands in a state without your books) gets
served nationwide-legal prediction markets (Novig, Kalshi, Polymarket,
ProphetX, Underdog) instead of DraftKings/BetMGM/Bet365/theScore Bet for
that card. Rather than discarding those cards, each one independently
falls back to its prediction-market pricing when NONE of PREFERRED_SPORTSBOOKS
priced it, and is tagged DataSource="COVERS_PREDICTION_MARKET" instead of
"COVERS" so the app can show that distinction to the user. Prediction
markets price as an implied-probability percentage (e.g. "o5.5 52%" or a
bare "52%" for a yes/no market like Anytime TD) rather than American odds --
see _parse_prediction_market_odds. Every row -- from either source -- then
gets its ModelFairOdds/EdgePct filled in by model.py's estimate_fair_odds().
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

from model import estimate_fair_odds
from parlay import prob_to_american

COVERS_URLS = {
    "NFL": "https://www.covers.com/sport/football/nfl/player-props",
    "CFB": "https://www.covers.com/sport/football/ncaaf/player-props",
}

# Only these sportsbooks' odds are kept as "real sportsbook" rows from live
# scrapes -- everything else Covers lists (FanDuel, Caesars, etc.) is
# silently dropped. Match against the lowercased, space-stripped slug Covers
# uses (see _format_sportsbook_name below for the slug -> display-name
# mapping). Edit this set to change which books show up as DataSource="COVERS".
PREFERRED_SPORTSBOOKS = {"draftkings", "betmgm", "bet365", "thescore", "thescorebet"}

# Nationwide-legal prediction markets Covers falls back to showing on a
# per-card basis when the visitor's location doesn't resolve to a state
# carrying your preferred sportsbooks (see the module docstring, and the
# SCRAPINGBEE_* comment below, for why that happens). Kept separate from
# PREFERRED_SPORTSBOOKS since their prices are implied-probability
# percentages, not American odds -- see _parse_prediction_market_odds.
# Rows sourced from these are only used for a card when none of
# PREFERRED_SPORTSBOOKS priced that specific prop, and are tagged
# DataSource="COVERS_PREDICTION_MARKET".
PREDICTION_MARKETS = {"novig", "kalshi", "kalshisports", "polymarket", "prophetx", "underdog"}

# ---------------------------------------------------------------------------
# Per-category "filtered projections" pages
# ---------------------------------------------------------------------------
# COVERS_URLS above only shows a curated ~50-pick "All Markets" feed per
# league, NOT the full market -- confirmed by drilling into individual
# category filters on the live site and finding 40-50+ players in a SINGLE
# category alone (e.g. CFB Passing Yards had 40 players by itself). Those
# category filters don't change the browser's URL bar -- they're a client-
# side fetch to a separate, discoverable endpoint:
#
#   https://www.covers.com/picks/filtered-league-projections/mlb/
#       <comma-separated game ids>/<market key>?country=<cc>&region=<r>
#
# ("mlb" in the path is a fixed literal segment Covers uses for this
# endpoint regardless of sport -- confirmed against real captured NFL and
# NCAAF requests, not a mistake to "fix".) The comma-separated game ids are
# just this week's slate for the league (the SAME list is reused across
# every category -- confirmed by comparing two different NCAAF categories
# side by side), and conveniently the main league page we already fetch
# embeds that exact list, plus the country/region Covers detected for the
# request, in one element:
#
#   <div id="market-filters" data-games="123,456,..." data-country="us"
#        data-region="in" class="covers-MarketButtons">
#
# So for each league, fetch_covers_props() fetches the main page once just
# to harvest that div (see _extract_market_filters), then makes one more
# request per category below to the filtered-projections endpoint, each of
# which returns the FULL, uncapped list of <section class="picks-card">
# rows for that single market -- the exact same markup _parse_prop_cards()
# already knows how to read (confirmed against a real captured row).
#
# NOTE: this means ~5 HTTP requests per league per refresh (1 for
# market-filters + 1 per category here) instead of 1 -- worth keeping an
# eye on ScrapingBee credit usage (see SCRAPINGBEE_* below) if you're on a
# metered plan or trial.
CATEGORY_MARKET_KEYS: dict[str, dict[str, str]] = {
    "NFL": {
        "Anytime TD": "nfl_game_player_score_touchdown",
        "Receiving Yards": "nfl_game_player_receiving_yards",
        "Receptions": "nfl_game_player_receiving_receptions",
        "Passing TDs": "nfl_game_player_passing_touchdowns",
    },
    "CFB": {
        # Unconfirmed -- no live CFB Anytime TD props existed yet to check
        # this key against a real request. If CFB TD-scorer props don't
        # show up once they're posted, this is the first thing to recheck.
        "Anytime TD": "ncaaf_game_player_score_touchdown",
        "Receiving Yards": "ncaaf_game_player_receiving_yards",
        # "recptions" (missing an 'e') is Covers' own market key, captured
        # verbatim from a real request -- not a typo introduced here.
        "Receptions": "ncaaf_game_player_recptions",
        "Passing TDs": "ncaaf_game_player_touchdown_passes",
    },
}

CATEGORY_PROJECTIONS_URL = (
    "https://www.covers.com/picks/filtered-league-projections/mlb/"
    "{games}/{market_key}?country={country}&region={region}"
)

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

REQUEST_TIMEOUT = 20

# If set, route the request through ScrapingBee instead of a plain
# requests.get(). Originally added for JS rendering, but it turns out
# Covers' real blocker is geography, not JavaScript: Covers decides which
# sportsbooks to render (DraftKings/BetMGM/Bet365/theScore Bet vs. the
# nationwide-legal prediction markets like Kalshi/Polymarket) based on the
# requester's US location, and a cloud server's data-center IP doesn't
# resolve to any betting-legal state. Routing through ScrapingBee's
# premium/residential proxy pool, geo-targeted to the US, gets us a real
# US residential IP instead -- which should land in a state where your
# preferred books actually operate (most populous states have all four by
# now), though which exact state isn't something we can pin down further
# without a pricier state-level-targeting proxy provider.
SCRAPINGBEE_API_KEY = os.environ.get("SCRAPINGBEE_API_KEY", "").strip()
SCRAPINGBEE_ENDPOINT = "https://app.scrapingbee.com/api/v1/"
SCRAPINGBEE_COUNTRY_CODE = os.environ.get("SCRAPINGBEE_COUNTRY_CODE", "us").strip()
# JS rendering costs extra ScrapingBee credits and Covers' props pages are
# confirmed server-rendered (see scraper.py's module docstring), so this
# defaults off to save credits; set SCRAPINGBEE_RENDER_JS=true to re-enable
# it if a future markup change ever makes it necessary again.
SCRAPINGBEE_RENDER_JS = os.environ.get("SCRAPINGBEE_RENDER_JS", "false").strip().lower() == "true"

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
    """Fetch raw HTML, optionally proxied through ScrapingBee -- geo-
    targeted to the US via a premium/residential proxy, so Covers sees a
    real US IP instead of a cloud data-center IP (see SCRAPINGBEE_* above
    for why that matters). Returns None (never raises) on any failure so
    callers can fall back cleanly."""
    try:
        if SCRAPINGBEE_API_KEY:
            params = {
                "api_key": SCRAPINGBEE_API_KEY,
                "url": url,
                "render_js": "true" if SCRAPINGBEE_RENDER_JS else "false",
                "premium_proxy": "true",
                "country_code": SCRAPINGBEE_COUNTRY_CODE,
            }
            resp = requests.get(SCRAPINGBEE_ENDPOINT, params=params, timeout=REQUEST_TIMEOUT)
        else:
            resp = requests.get(url, headers=REQUEST_HEADERS, timeout=REQUEST_TIMEOUT)

        if resp.status_code != 200:
            print(
                f"[scraper] {url} -> HTTP {resp.status_code}"
                f"{' (via ScrapingBee: ' + resp.text[:300] + ')' if SCRAPINGBEE_API_KEY else ''}",
                file=sys.stderr,
            )
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


# Matches Covers' current plain-text odds format, e.g. "u78.5 -110" or
# "o0.5 +145" -> (o|u) side, line, American odds. No JSON involved -- this
# is just the visible text inside the odds link/span.
_SIDE_LINE_ODDS_RE = re.compile(r"^([ou])\s*([\d.]+)\D*?([+\-]\d+)$", re.IGNORECASE)

# Matches a bare moneyline with no line number at all, e.g. "+145" or
# "-200" -- how Covers prices yes/no props like Anytime TD.
_MONEYLINE_RE = re.compile(r"^([+\-]\d+)$")

# Matches the odds-comparison "expand" button's data-tracking "text" field,
# e.g. "NO vs DET, Sun, Sep 13 . 1:00 PM ET" -> team, opponent, date/time.
# (This is the one JSON blob that's still present in the current markup.)
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
    "fanaticssportsbook": "Fanatics",
    "wynnbet": "WynnBET",
    "pointsbet": "PointsBet",
    "hardrockbet": "Hard Rock Bet",
    "thescore": "theScore Bet",
    "thescorebet": "theScore Bet",
    # -- Prediction markets (see PREDICTION_MARKETS above) --
    "novig": "Novig",
    "kalshi": "Kalshi",
    "kalshisports": "Kalshi",
    "polymarket": "Polymarket",
    "prophetx": "ProphetX",
    "underdog": "Underdog",
}


def _format_sportsbook_name(slug: str) -> str:
    key = slug.strip().lower()
    return _SPORTSBOOK_NAME_MAP.get(key, slug.strip().title())


def _clean_book_alt(alt: str) -> str:
    """Strip the trailing ' logo' suffix Covers appends to some (but not
    all) sportsbook <img alt> text, e.g. 'BetMGM logo' -> 'BetMGM'."""
    return re.sub(r"\s*logo\s*$", "", alt or "", flags=re.IGNORECASE).strip()


def _title_case_prop_type(text: str) -> str:
    """Title-case a badge like 'PASSING TDS' -> 'Passing Tds' the naive
    way, then fix up the common acronym Python's str.title() mangles:
    'Td'/'Tds' -> 'TD'/'TDs' (e.g. 'Anytime Td' -> 'Anytime TD')."""
    titled = text.title()
    titled = re.sub(r"\bTds\b", "TDs", titled)
    titled = re.sub(r"\bTd\b", "TD", titled)
    return titled


def _parse_side_line_odds(text: str) -> tuple[str, float, int] | None:
    """Parse the visible odds text on a link/span. Handles two shapes:

    - An over/under line, e.g. 'u78.5 -110' -> ('Under', 78.5, -110).
    - A bare moneyline with no line number at all, e.g. '+145' -- this is
      how Covers prices yes/no props like Anytime TD (there's no "line" to
      go over/under, just "does it happen"). Treated as ('Over', 0.5, odds)
      so it flows through the same schema as everything else, matching the
      Anytime TD convention already used in sample_data.py.

    Returns None for anything that doesn't match either shape -- including
    a book with no price posted, which Covers shows as a bare '-'."""
    cleaned = text.replace("\xa0", " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    m = _SIDE_LINE_ODDS_RE.match(cleaned)
    if m:
        side, line_str, odds_str = m.groups()
        try:
            return ("Over" if side.lower() == "o" else "Under", float(line_str), int(odds_str))
        except ValueError:
            return None

    m = _MONEYLINE_RE.match(cleaned)
    if m:
        try:
            return ("Over", 0.5, int(m.group(1)))
        except ValueError:
            return None

    return None


# Matches prediction-market pricing with an explicit side/line, e.g.
# "o5.5 52%" -> ('Over', 5.5, '52'). The number is the market's implied
# probability of that side happening (a percentage), NOT American odds.
_SIDE_LINE_PCT_RE = re.compile(r"^([ou])\s*([\d.]+)\D*?(\d+(?:\.\d+)?)\s*%$", re.IGNORECASE)

# Matches a bare probability percentage with no line at all, e.g. "52%" --
# how prediction markets like Kalshi/Novig price yes/no props such as
# Anytime TD (there's no "line" to go over/under, just "does it happen").
_MONEYLINE_PCT_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*%$")


def _parse_prediction_market_odds(text: str) -> tuple[str, float, int] | None:
    """Parse a prediction market's odds text (PREDICTION_MARKETS), which is
    an implied-probability percentage rather than American odds -- e.g.
    'o5.5 52%' -> ('Over', 5.5, <odds>), or a bare '52%' for a yes/no
    market. The percentage is converted straight to American odds via
    prob_to_american() so these rows flow through the exact same schema as
    sportsbook rows everywhere downstream (parlay pricing, the model, etc).

    Returns None for anything that doesn't match either shape, including an
    unpriced market (shown on Covers as a bare '-')."""
    cleaned = text.replace("\xa0", " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    m = _SIDE_LINE_PCT_RE.match(cleaned)
    if m:
        side, line_str, pct_str = m.groups()
        try:
            prob = min(max(float(pct_str) / 100.0, 0.0001), 0.9999)
            return ("Over" if side.lower() == "o" else "Under", float(line_str), prob_to_american(prob))
        except ValueError:
            return None

    m = _MONEYLINE_PCT_RE.match(cleaned)
    if m:
        try:
            prob = min(max(float(m.group(1)) / 100.0, 0.0001), 0.9999)
            return ("Over", 0.5, prob_to_american(prob))
        except ValueError:
            return None

    return None


def _parse_prop_cards(html: str, league: str, log_label: str | None = None) -> list[dict]:
    """Primary parser: extract prop rows from Covers' server-rendered
    <section class="picks-card"> prop cards (see module docstring for the
    confirmed markup shape). Returns one row per (player, prop, sportsbook)
    combination -- one from the "best odds" block plus up to several more
    from the odds-comparison panel, deduped when they're the same book.

    `league` is used for the emitted rows' "League" field (must be "NFL" or
    "CFB"). `log_label` is used only for the diagnostic print statements
    below -- pass something more specific (e.g. "NFL/Receiving Yards") when
    this is called once per category, so the funnel/sample-text lines in
    the log are distinguishable instead of all saying just "NFL"."""
    log_label = log_label or league
    soup = BeautifulSoup(html, "lxml")
    rows: list[dict] = []

    # Funnel counters -- printed at the end so we can see exactly which
    # step is dropping cards if this ever comes back empty again, instead
    # of just knowing the final row count.
    stats = {
        "total_cards": 0,
        "duplicate_cards_skipped": 0,
        "no_category_title": 0,
        "no_player_link": 0,
        "no_book_rows": 0,
        "no_best_odds_div": 0,
        "no_best_link": 0,
        "best_text_parse_fail": 0,
        "no_compare_div": 0,
        "compare_columns_seen": 0,
        "compare_text_parse_fail": 0,
        "cards_used_prediction_market_fallback": 0,
    }
    sample_texts: list[str] = []  # first couple of raw odds texts we actually saw, for debugging

    all_cards = soup.find_all("section", class_="picks-card")
    stats["total_cards"] = len(all_cards)

    # Covers' page has been observed rendering the SAME prop as two separate
    # <section class="picks-card" data-id="..."> elements (most likely a
    # responsive-layout duplicate -- e.g. one markup block per breakpoint,
    # both included in the server-rendered HTML). Each card carries a
    # data-id, so drop repeats of an id we've already processed -- otherwise
    # every book's odds for that prop get emitted twice, roughly doubling
    # raw row counts, before downstream grouping (best_line_per_prop in
    # app.py) incidentally collapses the duplicates back out again.
    seen_card_ids: set[str] = set()
    deduped_cards = []
    for card in all_cards:
        card_id = card.get("data-id")
        if card_id and card_id in seen_card_ids:
            stats["duplicate_cards_skipped"] += 1
            continue
        if card_id:
            seen_card_ids.add(card_id)
        deduped_cards.append(card)
    all_cards = deduped_cards

    for card in all_cards:
        category_title = card.find("div", class_="category-title")
        if not category_title:
            stats["no_category_title"] += 1
            continue
        player_link = category_title.find("a", class_="player-link")
        if not player_link:
            stats["no_player_link"] += 1
            continue
        player_name = player_link.get_text(strip=True)

        position_tag = category_title.find("span", class_="player-position")
        position = position_tag.get_text(strip=True).strip("() ") if position_tag else ""

        # Prop type: prefer the clean "RECEIVING YARDS" style badge; fall
        # back to parsing it out of the "u78.5 Receiving Yards" prediction
        # text if the badge isn't there for some reason.
        badge_tag = card.find("span", class_="_badge")
        if badge_tag and badge_tag.get_text(strip=True):
            prop_type = _title_case_prop_type(badge_tag.get_text(strip=True))
        else:
            prediction_tag = category_title.find("span", class_="prediction")
            prediction_text = prediction_tag.get_text(strip=True) if prediction_tag else ""
            m = re.match(r"^[ou]?[\d.]+\s+(.*)$", prediction_text, re.IGNORECASE)
            prop_type = m.group(1).strip() if m else prediction_text

        # Game: Covers already gives us this pre-formatted as "Away @ Home"
        # (e.g. "NO @ DET") -- no parsing needed.
        game_link = card.find("a", class_="projection-game-link")
        game = game_link.get_text(strip=True) if game_link else ""
        team, _, opponent = game.partition(" @ ")
        team, opponent = team.strip(), opponent.strip()

        # Kickoff time: the one place a data-tracking JSON blob still
        # exists is on the odds-comparison "expand" button.
        game_time_raw = ""
        game_time_iso = None
        expand_btn = card.find("button", attrs={"data-bs-target": True})
        if expand_btn:
            try:
                tracking = json.loads(expand_btn.get("data-tracking", ""))
                matchup_text = tracking.get("text", "")
            except (json.JSONDecodeError, TypeError):
                matchup_text = ""
            mm = _MATCHUP_TEXT_RE.match(matchup_text) if matchup_text else None
            if mm:
                _, _, game_time_raw = mm.groups()
                try:
                    game_time_iso = dateutil_parser.parse(game_time_raw, fuzzy=True)
                except (ValueError, OverflowError, TypeError):
                    game_time_iso = None

        book_rows = []       # priced by a PREFERRED_SPORTSBOOKS book
        fallback_rows = []   # priced only by a PREDICTION_MARKETS source
        seen = set()

        def _maybe_add_book(book_name_raw: str, odds_text: str, *, is_best: bool) -> None:
            if len(sample_texts) < 6:
                sample_texts.append(f"alt={book_name_raw!r} text={odds_text!r}")
            book_name = _format_sportsbook_name(_clean_book_alt(book_name_raw))
            if not book_name:
                return
            book_key = re.sub(r"[^a-z0-9]", "", book_name.lower())

            if book_key in PREFERRED_SPORTSBOOKS:
                parsed = _parse_side_line_odds(odds_text)
                target_list = book_rows
            elif book_key in PREDICTION_MARKETS:
                parsed = _parse_prediction_market_odds(odds_text)
                target_list = fallback_rows
            else:
                return  # not a book/market we track at all -- skip it

            if not parsed:
                if is_best:
                    stats["best_text_parse_fail"] += 1
                else:
                    stats["compare_text_parse_fail"] += 1
                return  # no usable price posted at this source for this prop
            side, line_val, odds_val = parsed
            dedupe_key = (book_key, side, line_val, odds_val)
            if dedupe_key in seen:
                return  # e.g. the "best odds" book also appears in compare-odds
            seen.add(dedupe_key)
            target_list.append({"side": side, "line": line_val, "odds": odds_val, "sportsbook": book_name})

        # -- Best-odds block: a single highlighted book + price.
        best_odds_div = card.find("div", class_="best-odd-container")
        if not best_odds_div:
            stats["no_best_odds_div"] += 1
        else:
            best_link = best_odds_div.find("a", class_="deeplink")
            if not best_link:
                stats["no_best_link"] += 1
            else:
                img = best_link.find("img")
                _maybe_add_book(img.get("alt", "") if img else "", best_link.get_text(" ", strip=True), is_best=True)

        # -- Odds-comparison panel: one column per sportsbook Covers
        # tracks. Lives inside the same card, addressable by the id the
        # expand button's data-bs-target points at.
        if expand_btn:
            target = expand_btn.get("data-bs-target", "")
            compare_div = soup.find(id=target[1:]) if target.startswith("#") else None
            if not compare_div:
                stats["no_compare_div"] += 1
            else:
                for column in compare_div.find_all("div", class_="compare-odds-column"):
                    stats["compare_columns_seen"] += 1
                    img = column.find("img")
                    link = column.find("a", class_="book-odds")
                    if not link:
                        continue
                    _maybe_add_book(img.get("alt", "") if img else "", link.get_text(" ", strip=True), is_best=False)

        # Prefer real sportsbook rows; only fall back to prediction-market
        # pricing for this card if NONE of your preferred books had a price
        # for it (see PREDICTION_MARKETS / module docstring).
        if book_rows:
            rows_to_emit, data_source = book_rows, "COVERS"
        elif fallback_rows:
            rows_to_emit, data_source = fallback_rows, "COVERS_PREDICTION_MARKET"
            stats["cards_used_prediction_market_fallback"] += 1
        else:
            stats["no_book_rows"] += 1
            continue  # couldn't recover any priced odds for this card, from either source

        for book in rows_to_emit:
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
                    "CoversLine": book["line"],
                    "SportsbookOdds": book["odds"],
                    "Sportsbook": book["sportsbook"],
                    "ModelFairOdds": book["odds"],  # placeholder -- estimate_fair_odds() fills this in
                    "EdgePct": 0.0,
                    "InjuryStatus": "",
                    "DataSource": data_source,
                }
            )

    print(
        f"[scraper] {log_label}: card funnel -- found {stats['total_cards']} <section class=picks-card>, "
        f"{stats['duplicate_cards_skipped']} were duplicate data-id repeats (skipped), "
        f"{stats['no_category_title']} missing category-title, {stats['no_player_link']} missing player-link, "
        f"{stats['no_book_rows']} had no usable odds from any tracked source, "
        f"{stats['cards_used_prediction_market_fallback']} fell back to prediction-market pricing "
        f"(no preferred sportsbook), {len(rows)} row(s) emitted.",
        file=sys.stderr,
    )
    print(
        f"[scraper] {log_label}: odds-extraction detail -- "
        f"no_best_odds_div={stats['no_best_odds_div']}, no_best_link={stats['no_best_link']}, "
        f"best_text_parse_fail={stats['best_text_parse_fail']}, no_compare_div={stats['no_compare_div']}, "
        f"compare_columns_seen={stats['compare_columns_seen']}, "
        f"compare_text_parse_fail={stats['compare_text_parse_fail']}.",
        file=sys.stderr,
    )
    for s in sample_texts:
        print(f"[scraper] {log_label}: sample odds text seen -- {s}", file=sys.stderr)

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


def _extract_market_filters(html: str) -> dict[str, str] | None:
    """Pull this week's game-id list, plus the country/region Covers
    detected for this request, out of the main league page's
    <div id="market-filters" data-games="..." data-country="..."
    data-region="..."> element (see the CATEGORY_MARKET_KEYS comment block
    above) -- so the per-category requests can be built without a separate
    lookup step. Returns None if that element/attribute isn't there (e.g.
    Covers changed this markup, or there's genuinely no slate this week)."""
    soup = BeautifulSoup(html, "lxml")
    filters_div = soup.find(id="market-filters")
    if not filters_div:
        return None
    games = filters_div.get("data-games", "").strip()
    if not games:
        return None
    return {
        "games": games,
        "country": filters_div.get("data-country", "us").strip() or "us",
        "region": filters_div.get("data-region", "").strip(),
    }


def _fetch_category_props(
    league: str, category: str, market_key: str, market_filters: dict[str, str]
) -> list[dict]:
    """Fetch + parse the full, uncapped list of props for a single market
    category (e.g. 'Receiving Yards') via Covers' filtered-league-
    projections endpoint. Returns [] (never raises) on any failure."""
    url = CATEGORY_PROJECTIONS_URL.format(
        games=market_filters["games"],
        market_key=market_key,
        country=market_filters["country"],
        region=market_filters["region"],
    )
    html = _get_html(url)
    if not html:
        print(
            f"[scraper] {league}/{category} ({market_key}): _get_html() returned nothing "
            "(request failed or non-200).",
            file=sys.stderr,
        )
        return []
    return _parse_prop_cards(html, league, log_label=f"{league}/{category}")


def fetch_covers_props(league: str, save_debug_html: bool = False) -> list[dict]:
    """Fetch + parse player props for one league ('NFL' or 'CFB').

    The main league page (COVERS_URLS) only shows a curated ~50-pick "All
    Markets" feed, not the full market -- see the CATEGORY_MARKET_KEYS
    comment block above. So this fetches that main page once purely to
    harvest this week's game ids + detected country/region (via
    _extract_market_filters), then makes one more request per category in
    CATEGORY_MARKET_KEYS[league] to get that category's full, uncapped
    list. Always returns a list (possibly empty) and never raises."""
    url = COVERS_URLS.get(league)
    if not url:
        raise ValueError(f"Unknown league '{league}'. Expected one of {list(COVERS_URLS)}.")

    html = _get_html(url)
    if not html:
        print(f"[scraper] {league}: _get_html() returned nothing (request failed or non-200).", file=sys.stderr)
        return []

    if save_debug_html:
        debug_dir = Path("debug_html")
        debug_dir.mkdir(exist_ok=True)
        (debug_dir / f"{league.lower()}.html").write_text(html, encoding="utf-8")

    market_filters = _extract_market_filters(html)
    if not market_filters:
        # Couldn't find this week's game-id list on the main page (markup
        # changed, or there's genuinely no slate posted yet) -- fall back
        # to whatever the main page's own (capped) card list has, same as
        # the old single-page approach, rather than coming back with
        # nothing at all.
        print(
            f"[scraper] {league}: couldn't find #market-filters data-games on the main page -- "
            "falling back to parsing the main page's own (capped, ~50-pick) card list instead. "
            "Diagnostics below are from that fallback page.",
            file=sys.stderr,
        )
        raw_card_count = html.count("picks-card")
        print(
            f"[scraper] {league}: fetched {len(html)} byte(s) of HTML, "
            f"'picks-card' appears {raw_card_count} time(s) in the raw response.",
            file=sys.stderr,
        )
        card_rows = _parse_prop_cards(html, league)
        if card_rows:
            return card_rows
        json_rows = _parse_next_data(html)
        normalized = [r for r in (_normalize_row(r, league) for r in json_rows) if r]
        if normalized:
            return normalized
        table_rows = _parse_html_tables(html)
        if table_rows:
            print(
                f"[scraper] {league}: found {len(table_rows)} raw table row(s) but no "
                "card/JSON data -- table parsing needs column mapping, see --debug output.",
                file=sys.stderr,
            )
        return []

    print(
        f"[scraper] {league}: market-filters found -- "
        f"{market_filters['games'].count(',') + 1} game(s) this week, "
        f"country={market_filters['country']!r}, region={market_filters['region']!r}.",
        file=sys.stderr,
    )

    all_rows: list[dict] = []
    for category, market_key in CATEGORY_MARKET_KEYS.get(league, {}).items():
        all_rows.extend(_fetch_category_props(league, category, market_key, market_filters))

    if all_rows:
        return all_rows

    # Every category request came back empty (e.g. nothing posted this
    # week for any of the 4 markets we care about yet) -- fall back to the
    # main page's own curated card list rather than nothing at all.
    print(
        f"[scraper] {league}: all category requests returned 0 rows -- falling back to the "
        "main page's own (capped) card list.",
        file=sys.stderr,
    )
    return _parse_prop_cards(html, league)


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
    if not df.empty:
        # Fill in real ModelFairOdds/EdgePct from the actual priced data
        # (multi-source consensus or a single-source vig haircut -- see
        # model.py), replacing the odds==fair-odds/0%-edge placeholders
        # each row was built with above.
        df = estimate_fair_odds(df)
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

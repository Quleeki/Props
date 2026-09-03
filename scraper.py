"""
scraper.py

Live player-prop scraper for:
    NFL  -> https://www.covers.com/sport/football/nfl/player-props
    NCAAF/CFB -> https://www.covers.com/sport/football/ncaaf/player-props

Covers.com renders its prop cards server-side (no hidden API call needed).
Each prop is a `<section class="picks-card ...">` shaped like this
(confirmed against real, live NFL markup -- Sept 2026 Week 1 props):

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
}


def _format_sportsbook_name(slug: str) -> str:
    key = slug.strip().lower()
    return _SPORTSBOOK_NAME_MAP.get(key, slug.strip().title())


def _clean_book_alt(alt: str) -> str:
    """Strip the trailing ' logo' suffix Covers appends to some (but not
    all) sportsbook <img alt> text, e.g. 'BetMGM logo' -> 'BetMGM'."""
    return re.sub(r"\s*logo\s*$", "", alt or "", flags=re.IGNORECASE).strip()


def _parse_side_line_odds(text: str) -> tuple[str, float, int] | None:
    """Parse the visible odds text on a link/span, e.g. 'u78.5 -110' ->
    ('Under', 78.5, -110). Returns None for anything that doesn't match --
    including a book with no price posted, which Covers shows as a bare
    '-' with no over/under letter or line number in front of it."""
    cleaned = text.replace("\xa0", " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    m = _SIDE_LINE_ODDS_RE.match(cleaned)
    if not m:
        return None
    side, line_str, odds_str = m.groups()
    try:
        return ("Over" if side.lower() == "o" else "Under", float(line_str), int(odds_str))
    except ValueError:
        return None


def _parse_prop_cards(html: str, league: str) -> list[dict]:
    """Primary parser: extract prop rows from Covers' server-rendered
    <section class="picks-card"> prop cards (see module docstring for the
    confirmed markup shape). Returns one row per (player, prop, sportsbook)
    combination -- one from the "best odds" block plus up to several more
    from the odds-comparison panel, deduped when they're the same book."""
    soup = BeautifulSoup(html, "lxml")
    rows: list[dict] = []

    # Funnel counters -- printed at the end so we can see exactly which
    # step is dropping cards if this ever comes back empty again, instead
    # of just knowing the final row count.
    stats = {
        "total_cards": 0,
        "no_category_title": 0,
        "no_player_link": 0,
        "no_book_rows": 0,
        "no_best_odds_div": 0,
        "no_best_link": 0,
        "best_text_parse_fail": 0,
        "no_compare_div": 0,
        "compare_columns_seen": 0,
        "compare_text_parse_fail": 0,
    }
    sample_texts: list[str] = []  # first couple of raw odds texts we actually saw, for debugging

    all_cards = soup.find_all("section", class_="picks-card")
    stats["total_cards"] = len(all_cards)

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
            prop_type = badge_tag.get_text(strip=True).title()
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

        book_rows = []
        seen = set()

        def _maybe_add_book(book_name_raw: str, odds_text: str, *, is_best: bool) -> None:
            if len(sample_texts) < 6:
                sample_texts.append(f"alt={book_name_raw!r} text={odds_text!r}")
            book_name = _format_sportsbook_name(_clean_book_alt(book_name_raw))
            if not book_name:
                return
            parsed = _parse_side_line_odds(odds_text)
            if not parsed:
                if is_best:
                    stats["best_text_parse_fail"] += 1
                else:
                    stats["compare_text_parse_fail"] += 1
                return  # no price posted at this book for this prop
            side, line_val, odds_val = parsed
            book_key = re.sub(r"[^a-z0-9]", "", book_name.lower())
            if book_key not in PREFERRED_SPORTSBOOKS:
                return  # not one of your preferred books -- skip it
            dedupe_key = (book_key, side, line_val, odds_val)
            if dedupe_key in seen:
                return  # e.g. the "best odds" book also appears in compare-odds
            seen.add(dedupe_key)
            book_rows.append({"side": side, "line": line_val, "odds": odds_val, "sportsbook": book_name})

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

        if not book_rows:
            stats["no_book_rows"] += 1
            continue  # couldn't recover any priced odds for this card

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
                    "CoversLine": book["line"],
                    "SportsbookOdds": book["odds"],
                    "Sportsbook": book["sportsbook"],
                    "ModelFairOdds": book["odds"],
                    "EdgePct": 0.0,
                    "InjuryStatus": "",
                    "DataSource": "COVERS",
                }
            )

    print(
        f"[scraper] {league}: card funnel -- found {stats['total_cards']} <section class=picks-card>, "
        f"{stats['no_category_title']} missing category-title, {stats['no_player_link']} missing player-link, "
        f"{stats['no_book_rows']} had no usable book odds, {len(rows)} row(s) emitted.",
        file=sys.stderr,
    )
    print(
        f"[scraper] {league}: odds-extraction detail -- "
        f"no_best_odds_div={stats['no_best_odds_div']}, no_best_link={stats['no_best_link']}, "
        f"best_text_parse_fail={stats['best_text_parse_fail']}, no_compare_div={stats['no_compare_div']}, "
        f"compare_columns_seen={stats['compare_columns_seen']}, "
        f"compare_text_parse_fail={stats['compare_text_parse_fail']}.",
        file=sys.stderr,
    )
    for s in sample_texts:
        print(f"[scraper] {league}: sample odds text seen -- {s}", file=sys.stderr)

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
        print(f"[scraper] {league}: _get_html() returned nothing (request failed or non-200).", file=sys.stderr)
        return []

    # Diagnostic: tells us whether the raw HTTP response even contains any
    # prop cards at all, vs. our parser failing to recognize them. If
    # "picks-card" never appears in the raw response, Covers is either
    # blocking/serving different content to non-browser requests, or the
    # props are injected client-side by JavaScript after page load (which
    # a plain requests.get() never executes) -- either way, that's a
    # different problem than a parsing bug.
    raw_card_count = html.count("picks-card")
    print(
        f"[scraper] {league}: fetched {len(html)} byte(s) of HTML, "
        f"'picks-card' appears {raw_card_count} time(s) in the raw response.",
        file=sys.stderr,
    )

    # Second-level diagnostic: 'picks-card' shows up in the raw text, but
    # _parse_prop_cards() (which specifically looks for <section
    # class="picks-card">) found nothing -- so find out what tag it's
    # actually attached to, and dump the raw markup around the first hit,
    # so we can see exactly what changed vs. the confirmed structure.
    if raw_card_count:
        debug_soup = BeautifulSoup(html, "lxml")
        any_tag_matches = debug_soup.find_all(attrs={"class": lambda c: c and "picks-card" in c})
        tag_names = sorted({t.name for t in any_tag_matches})
        print(
            f"[scraper] {league}: found 'picks-card' as a class on {len(any_tag_matches)} "
            f"element(s), tag name(s): {tag_names}.",
            file=sys.stderr,
        )
        first_idx = html.find("picks-card")
        snippet = html[max(0, first_idx - 300): first_idx + 700]
        print(f"[scraper] {league}: raw HTML around first 'picks-card' hit:\n{snippet}", file=sys.stderr)

    if save_debug_html:
        debug_dir = Path("debug_html")
        debug_dir.mkdir(exist_ok=True)
        (debug_dir / f"{league.lower()}.html").write_text(html, encoding="utf-8")

    # Primary strategy: Covers' real server-rendered div/card markup.
    card_rows = _parse_prop_cards(html, league)
    print(f"[scraper] {league}: _parse_prop_cards() extracted {len(card_rows)} row(s).", file=sys.stderr)
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

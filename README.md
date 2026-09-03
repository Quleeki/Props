# EdgeFinder — NFL + CFB Prop Dashboard & Parlay Builder

An interactive Streamlit dashboard for browsing NFL and CFB player props,
filtering out WR/RB longshots priced worse than +125, sorting by edge/odds/
position/kickoff, and pricing a live parlay ticket (with same-game
correlation) as you check boxes.

## What's in here

| File | Purpose |
|---|---|
| `app.py` | The Streamlit dashboard — filters, sorting, grid, parlay ticket. |
| `scraper.py` | Live scraper for the two Covers.com player-props pages (NFL + NCAAF), with a per-prop fallback to prediction-market pricing (see below). |
| `sample_data.py` | Fallback/demo data used whenever Covers returns nothing postable at all. |
| `model.py` | Fair-odds/edge estimation — multi-source consensus or a single-source vig haircut. Used for both live and sample data. |
| `parlay.py` | Odds math: American ↔ probability conversions, parlay pricing, SGP correlation. |
| `requirements.txt` | Python dependencies. |
| `.streamlit/config.toml` | Dark theme + headless server config. |

## Running it locally

```bash
python3 -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py
```

Your browser will open automatically to `http://localhost:8501`. Use the
sidebar to filter by league/position/prop type/sportsbook, search a player
name, and set the sort order. Check boxes in the grid to build a parlay —
the ticket at the bottom updates live.

## About the live data source

`scraper.py` targets:

- `https://www.covers.com/sport/football/nfl/player-props`
- `https://www.covers.com/sport/football/ncaaf/player-props`

**Update:** the scraper's parsing logic has been verified against a real,
live saved copy of Covers' MLB player-props page (which had live data,
unlike NFL/NCAAF at time of writing — Week 1 props hadn't posted yet).
Covers server-renders each prop as a `.category-title` "card" with a
`.best-odd-container` for the top line and a linked `.compare-odds-table`
for the rest of the sportsbooks — see the full breakdown in the comment
block at the top of `scraper.py`. This is a stable, non-JS-rendered
structure (no hidden API call needed), and `_parse_prop_cards()` targets it
directly. It correctly extracted player, position, matchup, kickoff time,
prop type/line, and odds across all 3 sportsbooks on the real test card.

Since NFL/NCAAF render the same way MLB does, this should "just work" the
moment Covers posts real Week 1 props — no further changes should be
needed. If it ever comes back empty despite props clearly being live on
the site, Covers likely changed their markup again; re-run:

```bash
python scraper.py --debug
```

This saves the raw HTML to `debug_html/nfl.html` and `debug_html/cfb.html`
and prints how many rows each parsing strategy found. Open those files,
find a player prop you recognize, and compare the surrounding HTML against
the structure documented at the top of `scraper.py`, then adjust
`_parse_prop_cards()` (or the older `JSON_KEY_ALIASES`/table-parsing
fallbacks) to match whatever changed.

Until then — or whenever Covers simply has no props posted yet for the
upcoming slate (the case for NFL/NCAAF as of writing) — the app
automatically falls back to the bundled sample data in `sample_data.py` and
shows a banner saying so. Nothing about the dashboard's filtering/sorting/
parlay logic changes based on which data source is active; they share the
exact same schema.

### Three-tier fallback: sportsbook → prediction market → sample data

The fallback actually happens **per prop, not per page load**. For each
card Covers renders:

1. If any of `PREFERRED_SPORTSBOOKS` (DraftKings/BetMGM/Bet365/theScore
   Bet) priced it, those rows are used — tagged `DataSource="COVERS"`.
2. Otherwise, if any of `PREDICTION_MARKETS` (Kalshi, Novig, Polymarket,
   ProphetX, Underdog) priced it, those rows are used instead — tagged
   `DataSource="COVERS_PREDICTION_MARKET"`. These are real, live
   probabilities from a nationwide-legal prediction market, just not from a
   traditional sportsbook you can necessarily bet at (some are
   exchange-style, not all are available in every state either). Prediction
   markets quote a probability percentage (e.g. `o5.5 52%`) rather than
   American odds; `_parse_prediction_market_odds()` in `scraper.py`
   converts that percentage straight to American odds so it flows through
   the exact same schema as everything else.
3. Only if a whole *page* comes back with nothing usable from either source
   (or the request fails outright) does the app fall back to the bundled
   `sample_data.py`.

The app's status banner and the grid's **Source** column both reflect which
tier actually served each row, so it's always clear whether you're looking
at a bettable sportsbook line, a prediction-market price, or illustrative
demo data.

## The fair-odds model (`model.py`)

Every row's **Edge %** comes from `model.py`, not from the sportsbook's own
number — a book's posted price already has its vig baked in, so treating
it as "fair" would make every prop show 0% edge and defeat the point of the
tool. Real de-vigging needs both sides of a market (Over *and* Under) from
the *same* book, which Covers' one-pick-per-card layout doesn't expose, so
`model.py` uses two practical stand-ins instead, applied per exact bet
(same player, same market/side, same line):

- **2+ sources price the same bet** (e.g. DraftKings *and* Bet365 both post
  a number for the same Over/Under): their implied probabilities are
  averaged. Independent books/markets set their vig independently, so
  averaging several real, independent prices is a reasonable — though not
  exact — stand-in for the vig-free "true" probability.
- **Only 1 source prices the bet**: there's nothing to average against, so
  a fixed assumed vig (`ASSUMED_SINGLE_SIDE_VIG`, 4.5% total — roughly what
  a standard `-110`/`-110` two-sided market carries — halved to account for
  a single side) is subtracted from that source's implied probability. This
  is a documented assumption, not a measurement.

**Edge % = (model's fair probability ÷ that row's own implied probability
− 1) × 100.** Positive means the posted price implies a *lower* chance of
winning than the model thinks is real — a good price for the bettor.
Negative means the opposite. This applies identically to live Covers.com
data and to `sample_data.py`'s demo data (each sample prop is priced at
2-4 books, so most sample rows get real multi-source consensus too) — "Edge
%" means the same thing no matter which data source is active.

### Required for real DraftKings/BetMGM/Bet365/theScore Bet odds: ScrapingBee geo-proxy

Covers.com decides which sportsbooks to render server-side based on the
*visitor's* US location, since DraftKings/BetMGM/Bet365 are licensed
state-by-state. A cloud server (like Streamlit Community Cloud) fetches
from a data-center IP with no real US state attached to it, so Covers
falls back to showing only nationwide-legal prediction markets instead
(Kalshi, Polymarket, Novig, ProphetX, Underdog) — confirmed by comparing
the scraper's logged output against a real browser's view of the same
page. This isn't a parsing bug; it's what Covers actually sends.

To get real sportsbook odds, route the request through a proxy with a US
residential IP via [ScrapingBee](https://www.scrapingbee.com/):

```bash
export SCRAPINGBEE_API_KEY="your-key-here"
```

On Streamlit Community Cloud, add it under the app's **Settings → Secrets**
instead of setting a local environment variable. No code changes needed —
`scraper.py` picks it up automatically and requests a premium/residential
proxy geo-targeted to the US (`country_code=us`). This should land the
request in *some* US state — most states with legal online sports betting
carry all four of your preferred books, so it'll very likely work, but
there's no guarantee of landing in one specific state without paying for a
pricier state-level-targeting proxy provider. If it lands in a state
without one of your books, that book will simply be absent from that
scrape, same as if a real visitor there saw the same thing.

Two more env vars tune this if needed:

- `SCRAPINGBEE_COUNTRY_CODE` (default `us`) — the proxy's country.
- `SCRAPINGBEE_RENDER_JS` (default `false`) — Covers' props pages are
  confirmed server-rendered (no JS needed to see the props), so this stays
  off to save ScrapingBee credits; flip it to `true` only if a future
  Covers markup change ever requires JS execution to see the content.

ScrapingBee's free trial (no credit card) includes a limited number of
credits to test this with; premium/residential proxy requests cost more
credits per call than a plain fetch, so keep an eye on usage if you move to
a paid plan — this was the cheaper alternative to Apify discussed when
scoping the project, but geo-targeted residential proxying isn't free.

## The +125 WR/RB longshot filter

`app.py` enforces (checkbox in the sidebar, on by default):

> WR/RB props are dropped if their sportsbook odds are worse than +125.
> QB and TE props are never capped.

## Parlay pricing

Two numbers are shown for any selected combination of legs:

- **Sportsbook Parlay Payout** — straight multiplication of the actual
  posted sportsbook odds for each leg (what a book would actually pay you).
- **Model Fair Parlay Odds** — same combination using each leg's *model fair
  odds*, with a same-game correlation multiplier (adjustable 1.05x–1.25x in
  the sidebar, default 1.15x) applied whenever 2+ selected legs share a
  game — e.g. a QB's Anytime TD and his TE's Anytime TD in the same game are
  not independent events.
- **Parlay Value Edge** — the % difference between those two, i.e. whether
  the combination you built still carries positive expected value once
  correlation is accounted for.

## Deploying it for free (Streamlit Community Cloud)

Netlify **cannot** host this — it's a static-site/serverless-function host,
and this app needs a persistent Python process. Streamlit Community Cloud
is the free option built for exactly this:

1. Push this repo to GitHub (see below).
2. Go to https://share.streamlit.io and sign in with your GitHub account.
3. Click **New app**, pick this repo, branch `main`, and set the main file
   to `app.py`.
4. Click **Deploy**. It installs `requirements.txt` and gives you a public
   URL in under a minute.
5. If you set `SCRAPINGBEE_API_KEY`, add it under the app's **Settings →
   Secrets** instead of committing it to the repo.

## Pushing this repo to your GitHub (Quleeki account)

This sandbox environment isn't authenticated as you on GitHub, so I
couldn't create/push the repo directly — you'll need to run these two
steps yourself (takes under a minute):

1. Create an empty repo at https://github.com/new under your account
   (e.g. name it `football-prop-bets`). **Don't** initialize it with a
   README/license/gitignore — leave it empty so there's nothing to
   conflict with.
2. From this project's folder, run:

   ```bash
   git remote add origin https://github.com/Quleeki/football-prop-bets.git
   git branch -M main
   git push -u origin main
   ```

   (Swap in whatever repo name you actually used in step 1.)

After that, point Streamlit Community Cloud at the new repo as described
above.

## Responsible gambling

This tool is for informational/entertainment purposes — it does not
guarantee outcomes, and "edge"/"fair odds" figures are model estimates, not
certainties. 21+. If you or someone you know has a gambling problem, call or
text the National Problem Gambling Helpline at 1-800-522-4700.

# EdgeFinder — NFL + CFB Prop Dashboard & Parlay Builder

An interactive Streamlit dashboard for browsing NFL and CFB player props,
filtering out WR/RB longshots priced worse than +125, sorting by edge/odds/
position/kickoff, and pricing a live parlay ticket (with same-game
correlation) as you check boxes.

## What's in here

| File | Purpose |
|---|---|
| `app.py` | The Streamlit dashboard — filters, sorting, grid, parlay ticket. |
| `scraper.py` | Live scraper for the two Covers.com player-props pages (NFL + NCAAF). |
| `sample_data.py` | Fallback/demo data used whenever the scraper returns nothing. |
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

### Optional: JS rendering via ScrapingBee

If Covers blocks plain requests or the props are loaded client-side after
page load, set an environment variable and the scraper will route requests
through ScrapingBee (JS rendering + rotating proxies) instead of a bare
`requests.get()`:

```bash
export SCRAPINGBEE_API_KEY="your-key-here"
```

No code changes needed — `scraper.py` picks this up automatically. (This
was the cheaper alternative to Apify discussed when scoping the project;
ScrapingBee's free/freelancer tier should comfortably cover checking two
pages periodically.)

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

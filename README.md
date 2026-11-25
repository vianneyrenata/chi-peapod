# CHI Peapod

A small web app for finding people at CHI 2027 whose work is close to yours.

Every author and session chair in the official programme is a dot on a map. The map comes from
embedding their paper titles and abstracts, so people who work on similar things end up near each
other. You look yourself up, see who's nearby, read what they're presenting, and save the talks you
want to go to. There's no messaging on purpose — the point is to give you a reason to walk up to
someone in the hallway.

I built the first version for HFES 2026 (HFE Peapod). This is the CHI version, entered in the
CHI Digital Experience competition. CHI 2027 is May 10–14 in Pittsburgh.

## Where the data comes from

Everything about people and talks comes from the public CHI programme feed
(https://chidigitalexperience.org/programme-data/), which is CC BY-NC-SA 4.0. The app polls it and
rebuilds its own files from it; none of that data is in this repo. Anything you type into your own
profile stays on the app's server and nowhere else.

## Running it

You need Python 3.12 or newer, PostgreSQL, and about 3 GB of RAM for the first build (it embeds
~10k people and fits a UMAP).

```bash
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
createdb chipeapod

# Starts the API. The first start creates the tables, pulls the programme feed, writes
# authors.csv / program.csv, and runs the first embedding + map build. About a minute.
HFES_DATABASE_URL=dbname=chipeapod python3 server.py --port 8000

# In another terminal: serves the page and forwards /api to :8000, standing in for nginx.
python3 devproxy.py
```

Then open http://localhost:8765.

`HFES_FEED_EDITION` picks the feed: `chi2026` (frozen, the default while developing), `sandbox`
(synthetic, full of edge cases), or `chi2027` once it's live. `HFES_FEED_CHECK_SECONDS` is how
often the server re-checks the feed (900 = every 15 minutes; 0 = never).

To rebuild the map by hand: `HFES_DATABASE_URL=dbname=chipeapod python3 embed_umap.py full`.
`RECOMPUTE.md` is for doing that offline and copying the result to a server.

Checks: `python3 test_chi_feed.py` runs the feed converter over the sandbox's edge cases (nulls,
HTML entities, cancelled sessions, odd Unicode). `HFES_FEED_CHECK_SECONDS=0 python3 test_server.py`
is the API selftest and needs the database.

## Deploying

`deploy/` has what I use for one Ubuntu box behind CloudFront: the nginx config, two systemd units
(the API and a warm embedding worker so new sign-ups get placed in about a second), and the
bootstrap and deploy scripts. Settings live in `/etc/hfes-match.env`.

The `HFES_*` names and `hfes-match` paths are left over from the HFES version and shared with its
deploy tooling. Only what users see says CHI.

`CLAUDE.md` explains how the pieces fit together. `CHECKLIST.md` is what's still open.

## How this was made

The code was written with Anthropic's Claude (Claude Code), with me directing, reviewing and
testing every change — fully AI-assisted coding, in other words. The idea, the overall design and
architecture, the interaction flow, and the peapod mascot and logo (`img-asset/`) are my own
original work. If you find something wrong, the person to blame is me, not the model.

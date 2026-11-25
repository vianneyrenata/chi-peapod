# CHI Peapod

Find the people at ACM CHI 2027 (Pittsburgh, May 10–14) whose work is closest to yours. Every author
and session chair in the official programme gets a dot on a map built from their titles and abstracts;
you find yourself, see your nearest neighbours, and save programme items. An entry for the CHI Digital
Experience competition, adapted from HFE Peapod (HFES 2026).

Data comes from the public CHI programme feed (https://chidigitalexperience.org/programme-data/,
CC BY-NC-SA 4.0). Nothing from the feed is committed; the app downloads and rebuilds it.

## Run it locally

Needs Python 3.12+, PostgreSQL, and ~3 GB of RAM for the first build (sentence-transformers + UMAP
over ~10k people).

```bash
# 1. deps + database
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
createdb chipeapod

# 2. start the API. On first start it creates the tables, pulls the programme feed
#    (chi2026 by default; HFES_FEED_EDITION=sandbox or chi2027), writes authors.csv /
#    program.csv, and runs the first full embedding + map build (~1 min). Watch the log.
HFES_DATABASE_URL=dbname=chipeapod python3 server.py --port 8000

# 3. in a second terminal: serve the page and forward /api to :8000 (local stand-in for nginx)
python3 devproxy.py
# → http://localhost:8765
```

To rebuild the map by hand: `HFES_DATABASE_URL=dbname=chipeapod python3 embed_umap.py full`.
`RECOMPUTE.md` covers rebuilding offline and copying the result to a server.

Checks: `python3 test_chi_feed.py` (feed converter on sandbox-style edge cases) and
`HFES_FEED_CHECK_SECONDS=0 python3 test_server.py` (API selftest; needs the database).

## Deploy

`deploy/` has the nginx config, systemd units (API + warm embedding worker), bootstrap and deploy
scripts for a single Ubuntu box behind CloudFront. Runtime settings live in `/etc/hfes-match.env`:
`HFES_DATABASE_URL`, `HFES_FEED_EDITION`, `HFES_FEED_CHECK_SECONDS` (feed poll interval; 900 for
every 15 min). The `HFES_*` names are shared with HFE Peapod's tooling and are not a typo.

`CLAUDE.md` explains how the pieces fit; `CHECKLIST.md` is the open work.

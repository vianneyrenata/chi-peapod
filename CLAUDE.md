# CLAUDE.md

**CHI Peapod**: HFE Peapod (`../hfe-peapod`) adapted for ACM CHI 2027 (May 10–14, 2027, Pittsburgh) and the
CHI Digital Experience competition. Forked from hfe-peapod on 2026-09-28 as a **fresh repo with no history**,
because the competition requires the code to be public and hfe-peapod's history holds HFES attendee bios,
embeddings, server addresses and incident notes. Don't copy data files or hfe-peapod's CLAUDE.md in.
hfe-peapod's CLAUDE.md is still the reference for how the shared machinery works (warm worker, frozen-map
incremental placement, Student/Hobby views, name matching, the job ledger). Fixes don't flow between the two
repos automatically. Port them by hand.

Identifiers stay `HFES_*` / `hfes-match` (env vars, systemd units, `/opt/hfes-match`) because they're shared
with hfe-peapod's deploy tooling. Only user-facing text says CHI.

## Data comes from the CHI programme feed, not exports

`chi_feed.py` reads https://chidigitalexperience.org/programme-data/ (JSON, schemeVersion 7, no key)
and writes the same files the HFES version used, so the frontend and `embed_umap.py` barely changed:

- `authors.csv`: one row per person with a paper or a chaired session. The displayed bio
  (`Interest Profile`) is "**At CHI …**" plus their titles. **No abstracts in this file**, because
  every browser downloads it (9.7k people = 6.5 MB, 1.1 MB gzipped).
- `program.csv`: one row per (content, session), times in the feed's `conference.timeZoneName`.
- `embed_text.json`: `{person_id: [displayed_bio, titles+abstracts]}`. Server-side only.
  `embed_umap.feed_text()` embeds the abstracts **only while the text still equals the feed bio**:
  claiming a profile re-posts the displayed bio unchanged, and that must not degrade the embedding.
  A self-written bio is embedded as written.

`server.py`'s `periodic_feed_check()` polls `version.json` every `HFES_FEED_CHECK_SECONDS` (60; 0 = off)
and fetches the immutable `revision_url` only when `revision` moves (`feed_revision.json` records the last
one). It holds `REBUILD_LOCK` while rewriting, because jobs write `authors.csv` too. It then re-applies
every database edit (`materialize_db_edits()`, since the rewrite just dropped them), reloads the catalogue,
and queues an **incremental** placement for people with no embedding yet. Existing dots never move. Changed
abstracts wait for a deliberate `full`. First sync with no embeddings runs `full`. Chairs with no papers
have no text and aren't placed. A feed outage only logs; the last good files keep serving.
`HFES_FEED_EDITION` picks the edition: `chi2026` (frozen, default for development), `sandbox`, or
`chi2027` once it's live in April 2027.

Data licence: **CC BY-NC-SA 4.0**. The attribution line under the app card is required; derived data
(CSVs, embeddings, positions) carries the same licence. The generated files are gitignored.

## Commands

```
python3 chi_feed.py [--edition chi2026] [--force]   # one sync (no DB needed; edits re-applied if it's up)
HFES_DATABASE_URL=dbname=chipeapod python3 embed_umap.py full
HFES_DATABASE_URL=dbname=chipeapod python3 server.py --port 8000
python3 -m http.server 8765                          # static frontend
HFES_FEED_CHECK_SECONDS=0 python3 test_server.py     # selftest
```

If you touch `index.html`'s inline script, `node --check` the extracted script.

## Measured at CHI scale (chi2026 feed, 2026-09-28, laptop)

9,754 people / 3,392 programme rows; 16,796 schedule entries, all parsed by the frontend's `SCHEDULE_RE`.
`full`: 54 s, **peak RSS 2.1 GB**. The HFES t3.small (2 GB, plus the ~790 MB warm worker) won't fit;
plan for ≥4 GB. `umap_positions.json` is **23.8 MB (3.3 MB gzipped)**, mostly 20 named top-matches per
person, and every browser downloads it; trimming that is open. Headless Chrome loads Home with 9.7k dots
in ~2 s locally, with no JS errors. Poller e2e: 3 "new" people placed, 50 sampled existing dots
byte-identical. Not done: rerunning the name-lookup thresholds (`SURNAME_RARE_MAX`, `GIVEN_RARE_MAX`,
tuned on 1,648 HFES names) against CHI's 9.7k names with 134 exact collisions.

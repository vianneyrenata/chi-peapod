# CHI Peapod — what needs doing

Status as of 2026-09-28. `[x]` done, `[ ]` open, `[?]` needs a decision first.
CHI 2027: May 10–14, Pittsburgh. The chi2027 feed goes live in **April 2027**; until then, build against `chi2026`.

## Data and content
- [x] Feed → `authors.csv` / `program.csv` converter (`chi_feed.py`), times in the conference timezone
- [x] Poller: new revision → re-sync, re-apply user edits, place only new people
- [x] Abstracts embedded server-side (`embed_text.json`), not shipped to browsers
- [?] Displayed bio for feed people: currently "**At CHI …**" + titles. Options: friendlier framing
      ("Presenting at CHI:"), abstract excerpt, or generated bios (accuracy / no-review / data-protection
      concerns). Placeholder text doesn't affect the map, as long as `chi_feed.py` writes the same text
      to both files
- [ ] Nudge on claim: "add a line about yourself" (most feed bios are just titles)
- [ ] Session Chair-only people (no papers): shown in the list, not on the map. Confirm that's fine
- [ ] People dropped from a later feed revision (withdrawn papers) stay in the embedding files.
      Check the frontend hides them cleanly (it should: it only shows `authors.csv` rows)
- [ ] Switch `HFES_FEED_EDITION=chi2027` in April 2027, then one deliberate `full` rebuild
- [ ] `Program URL` is the DOI. Check whether the official CHI programme site has a better per-talk link
- [ ] Content types: decide which to hide from "Top programs" (Breaks are dropped; Meet-Ups, Plenary,
      Global Plaza?). HFES filtered Registration and lab posters the same way

## Download size and speed (first load today: 4.6 MB gzipped)
- [ ] **Slim `umap_positions.json`**: drop names from `top_matches`, keep 5–10 matches, round x/y to
      2 decimals. 3.2 MB → 0.28 MB (top-5) / ~0.5 MB (top-10). With top-5, co-author filtering can leave
      1–2 visible matches until "Show more"
- [ ] Slim `authors.csv` (1.07 MB): `Schedule Detail` repeats what `program.csv` already has
- [?] Gradual map (draw dots from positions before the CSV lands). Only if the slim files still feel slow
- [ ] Measure on a throttled phone profile (1 Mbps) after slimming
- [ ] CloudFront TTL for data files: 1 h at HFES; new signups should appear sooner during the event

## Scale
- [ ] Server with ≥4 GB RAM: `full` peaks at 2.1 GB, and the warm worker idles at ~790 MB
      (HFES's t3.small is 2 GB)
- [ ] Raise the worker's `MemoryMax=1300M` to fit a 9.7k-person full fit
- [ ] `match_page()` cache: ~39 KB per person who opens their matches, ~380 MB if all 9.7k do
- [ ] Re-run the name-lookup simulation on CHI names (9.7k, 134 exact collisions); re-tune
      `SURNAME_RARE_MAX` / `GIVEN_RARE_MAX`
- [ ] Map density: 9.7k dots read as one blob with outliers. Dot size / clipping outliers?
- [ ] Load test at CHI scale (Locust, read-only)

## Competition hurdles (pass/fail) — https://chidigitalexperience.org/judging/
- [ ] **Open source**: pick a licence (code), publish this repo. Already clean: no data, no HFES
      history, no personal key paths. FAQ contact emails are deliberate
- [x] Data licence attribution line in the app (CC BY-NC-SA 4.0)
- [?] Derived-data licence: user-written bios live in the same `authors.csv` as feed-derived data,
      and ShareAlike covers derived data. Keep user text in a separate file?
- [?] **Security**: anyone can edit any profile by ID (accepted at HFES). A jury may call that an
      obvious vulnerability. Options: a per-device claim token, email confirmation, or accept + explain
- [ ] Restrict the origin to CloudFront (HFES item never done)
- [?] **Data protection** (requirements still "being finalised"): server stores bios / side interests /
      student flag; `draft-bio` web-searches a named person via a third party. Revisit when the rules
      are published; `draft-bio` is the likeliest to need changing or turning off
- [ ] Privacy notice in the FAQ: what's stored, where, for how long, who to ask for deletion

## Scoring (UX 25, novelty 25, inclusion & accessibility 25, CHI relevance 15, technical 10)
- [x] Dark Mode, keyboard access, dialog focus, map label, contrast (ported from hfe-peapod 2026-09-28)
- [ ] VoiceOver pass on a real iPhone
- [ ] Recheck 320 px layout with CHI's longer names / affiliations
- [ ] Look at earlier CHI apps for the novelty story

## Metrics (anonymous, see the hfe-peapod thread 2026-09-28)
- [ ] Per-browser active half-days: morning / afternoon split at **~2:00 pm** (check CHI's lunch
      times in the feed; HFES's was 12:00–1:30), sent as "Nth half-day" pings with no ID
- [ ] "Show more" count per half-day ÷ active browsers
- [ ] Saved programs: count per programme item, not per person. Saved contacts: totals only
- [ ] Ping endpoint excluded from nginx access logs (IP + timestamp would de-anonymise it)
- [ ] Wipe metric counts together with test profiles before the conference
- [ ] Low-key exit survey link (4 questions: how you met people / met someone via Peapod / use again / open)

## Before the conference
- [ ] Wipe test profiles + their CSV rows + embeddings (same as the hfe-peapod pre-conference wipe)
- [ ] Alert email: new Gmail app password (broken on hfe-peapod as of 2026-09-28)
- [ ] Anthropic spend limit, if `draft-bio` stays
- [ ] Update FAQ "who runs this" + shutdown date (set to May 14, 2027) once final

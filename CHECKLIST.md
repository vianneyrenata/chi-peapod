# CHI Peapod — what needs doing

Status as of 2026-09-28. `[x]` done, `[ ]` open, `[?]` needs a decision first.
CHI 2027: May 10–14, Pittsburgh. The chi2027 feed goes live in **April 2027**; until then, build against `chi2026`.

## Session log
**2026-11-03**
- Ran `chi_feed.py` on the **sandbox** edition (revision 2, CHI 2025 data + seeded edge cases).
  Fixed in `chi_feed.py`, with `test_chi_feed.py` as the check: explicit `null` for `authors` /
  `affiliations` / `chairIds` / `contentIds` / `doi` crashed the converter; **feed epochs are local
  wall-clock stored as UTC** (CHI 2025's 8:45 plenary = 08:45Z; chi2026 was showing 11 am for 9 am
  sessions, Pittsburgh would have shown 5 am), now formatted in UTC; `addons.cancelled` sessions get a
  `CANCELLED:` prefix (kept visible); HTML entities unescaped, BOM/zero-width marks and a stray leading
  diacritic stripped in `_clean`. Fine already: null room → TBA, 45-author paper, CJK/RTL/emoji,
  4000-char abstract, overlapping sessions, relocated/rescheduled sessions.
  `deploy/nginx.conf`: `program.csv` now `max-age=300` (other data files stay 1 h). For a 15-min live
  pull set `HFES_FEED_CHECK_SECONDS=900`. Not tested: a revision moving while the server runs (the
  sandbox has no live changes).

**2026-10-06**
- Ported hfe-peapod 09-28→10-06 (see CLAUDE.md "Sync with hfe-peapod" and `../hfe-peapod/CHI-SYNC.md`).
  Still open from that: trim `umap_positions.json` before lowering the data TTL to 30 s; decide what the
  claimed circle should mean at CHI; CHI-specific copyright/licence line in the page header.

**2026-09-30**
- Academic accent is now **CHI blue** (light `#1f6fd1` / text+buttons `#1858b0`, 6.9:1 white-on; dark
  `#4b8fe8` / `#6aa8f5` on blue-tinted charcoal). Student purple, Hobby orange unchanged; green kept only
  for success states (found banner, "presenting" badge, success button). Mascot stays green (it's a pea)

**2026-09-28** (uncommitted at time of writing; see `git diff`)
- User-facing HFES leftovers → CHI: DOI button label, FAQ "technical groups" line, interests placeholder.
  Internal `HFES_*` / `hfes-match` identifiers deliberately kept (shared deploy tooling)
- FAQ: new "Where does the conference data come from?" (© ACM CHI, CC BY-NC-SA, derived data same licence)
- **Light mode is the default**; dark mode is a button in your own profile popup (next to Saved
  Contacts / Saved Programs), remembered in `localStorage.peapod_theme`. Space above "Build my profile"
- Measured the data anomalies (section below) and fixed the ones that needed no decision:
  co-authors never fill the baked top-20 (`embed_umap.coauthor_index`, now shared with server.py, with a
  title fallback for the 773 unscheduled people); `full` embeds each distinct text once (4,249 of 9,725;
  54 s → 38 s, RSS unchanged at 2.1 GB); stacked map dots nudged apart; one suggestion per programme
  item with all its times on the card; Awards + Student Mentoring no longer suggested; no-DOI items
  link to programs.sigchi.org; open tabs refetch data after 10 min in the background; Saved Programs
  count ignores withdrawn items
- Run locally: `server.py --port 8000` + `python3 devproxy.py` (serves the page and forwards `/api`,
  standing in for nginx) → http://localhost:8765
- **Waiting on a decision:** Top Programs ranking (embed programme items, or at least drop the hash
  noise + fix the FAQ); keep or drop the "55%"; bio cleanup on claim (store only changed fields)

## Data and content
- [x] Feed → `authors.csv` / `program.csv` converter (`chi_feed.py`); feed epochs are wall-clock-as-UTC, formatted in UTC
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
- [x] `Program URL` is the DOI; button reads "View paper (DOI) ↗" (was "View on HFES site")
- [x] 61 items have no DOI (36 Journals, 16 Posters, 5 Awards, 4 Keynotes): `chi_feed.py` links them to
      `https://programs.sigchi.org/<chi>/<2026>/program/content/<Presentation ID>` (slug from the edition,
      so chi2027 follows automatically; checked it renders), button reads "View in CHI programme"
- [ ] 773 people have papers in no scheduled session (empty `Schedule Detail`): they get a bio and a dot
      but no time/room. Check what these are in the feed (journal papers? withdrawn-from-schedule?)
- [x] FAQ entry on where the data comes from + CC BY-NC-SA
- [x] Content types hidden from "Top programs": Breaks (dropped in the feed), SIGCHI Awards, Student
      Mentoring Program. Meet-Ups, Workshops, Plenary kept

## Data anomalies (measured on chi2026, 2026-09-28)
- [x] **Identical texts → stacked dots.** Only 4,249 distinct texts for 9,725 embedded people (single-paper
      co-authors share titles + abstract). `full` now embeds/fits each distinct text once; the map nudges
      each dot ≤1.5 units by a hash of its Person ID. Still open: say "N co-authors here" in the popup?
- [x] **Zero visible matches (bug).** Co-authors filled the whole baked top-20 for 106 people. Fixed at
      the source: `full` and incremental placement skip co-authors before taking 20. Verified: 0 of 9,725
      lists contain a co-author, all have 20, none starts with a 100% identical text
- [ ] ~~Co-authors matched by name~~: wrong, both `COAUTHORS` and server.py group by Person ID. What *is*
      name-based: Top Programs hides "your own talk" by comparing author names, so a namesake's talks
      are hidden from you too (134 duplicate names). Minor
- [ ] **Top Programs aren't embedding-based**: keyword overlap ×10 plus `hashCode(title) % 20` noise, so
      one shared word can lose to a lucky hash. The FAQ says "same idea" as Top Matches, which isn't true
- [x] **Duplicate Top Programs.** 802 items run in >1 session. Suggestions now show each item once
      ("(+1 more time)"), the card lists every time. Saved Programs still shows the first session only
- [x] **Non-talk items in Top Programs**: SIGCHI Awards (5) and Student Mentoring Program (80) are no longer
      suggested (`NOT_SUGGESTED_TYPES`); Meet-Ups (32) and Workshops (133) stay. Still in the data
- [ ] **Truncation**: MiniLM reads ~190 words, texts are cut at `EMBED_CHARS` (max 241 words, median 163).
      Titles come first, so prolific authors (13 people with ≥10 papers, max 17) are embedded from titles
      alone and single-paper authors from title + most of one abstract. Which papers fill the window
      depends on feed order. Acceptable, but say so if asked
- [ ] **Score reads as a probability.** "55%" is cosine × 100, not a chance or a percentile, and there's
      no minimum: everyone gets their nearest neighbours however far away. Options: show rank only,
      a percentile, or a floor ("no close matches yet") calibrated on the score distribution
- [ ] **Names**: 314 non-ASCII names (name matching already folds accents, `index.html:1258`; confirm the
      search box uses it), some ALL-CAPS names (YI-CHIEH LEE)
- [ ] Items with >20 authors (7, max 30): long author lines on cards; check the 320 px layout
- [ ] 29 chairs-only people: in the list, not on the map (already noted above)

## Programme changes (rooms, times, withdrawals) — checked 2026-09-28
Already fine: every new feed revision rewrites `program.csv` + `authors.csv` whole; saved programs are
stored as Presentation IDs, so a moved room/time shows up on the saved list; user edits come back from
Postgres; existing dots never move.
- [x] **Open tabs refetch**: coming back to the foreground with data older than 10 min re-runs
      `loadRealData(true)` (a failed refresh keeps what's on screen)
- [x] **CloudFront TTL** on `program.csv`: `max-age=300` in nginx (2026-11-03); poll + 5 min + 10-min tab refetch
- [ ] **Claimed-but-unedited bios freeze.** Claiming stores the feed bio in Postgres as `s_profile`, which
      then overrides the feed forever: a later title fix or added paper never shows, and at the next
      `full` the text no longer equals the feed bio, so `feed_text()` treats it as self-written and drops
      the abstracts. Fix: don't store `s_profile` when the posted bio equals the current feed bio
      (or store "unchanged" and let the feed win)
- [ ] **Same for name/affiliation**: claiming snapshots them; later feed corrections are ignored for
      that person. Only store fields the person actually changed?
- [x] **Withdrawn authors who claimed** come back via `materialize_db_edits()` as rows with no
      programme. Decided 2026-09-28: keep them (they may still attend)
- [x] **Withdrawn saved items**: the Saved Programs count now only counts items still in the programme
      (the list already skipped them). No "no longer in the programme" notice yet
- [ ] **New papers / changed abstracts for existing people** don't move their dot until a deliberate
      `full`. Plan one `full` after the final programme (early May), before people rely on the map
- [ ] Never tested with a real room/time change while the server runs. Sandbox converts clean
      (2026-11-03) but has no live revisions; simulate by editing `feed_revision.json` and re-polling

## Download size and speed (first load today: 4.6 MB gzipped)
- [ ] **Slim `umap_positions.json`**: drop names from `top_matches`, keep 5–10 matches, round x/y to
      2 decimals. 3.2 MB → 0.28 MB (top-5) / ~0.5 MB (top-10). With top-5, co-author filtering can leave
      1–2 visible matches until "Show more"
- [ ] Slim `authors.csv` (1.07 MB): `Schedule Detail` repeats what `program.csv` already has
- [?] Gradual map (draw dots from positions before the CSV lands). Only if the slim files still feel slow
- [ ] Measure on a throttled phone profile (1 Mbps) after slimming
- [ ] CloudFront TTL for data files: 1 h at HFES; new signups should appear sooner during the event

## Scale
- [ ] Server with ≥4 GB RAM: `full` peaks at 2.1 GB (still, after the distinct-text change; 38 s), and the warm worker idles at ~790 MB
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

# Recompute and traffic policy

The web server never embeds during an attendee request.  It has three separate
lanes:

1. **Read lane:** Nginx serves the HTML and static conference files directly;
   the API keeps the attendee and precomputed-match indexes in memory. This is
   the only lane used by normal browsing and works well for the expected
   100–200 concurrent people.
2. **Write lane:** profile and saved-contact updates use SQLite in WAL mode.
   WAL permits readers while a brief write is committed. A profile update adds
   the changed profile to a durable `queued` rebuild job's pending list; more
   updates coalesce into that same job (deduped by person, latest text wins).
3. **Compute lane:** after a 3-minute quiet period, the production API places
   the whole pending list in ONE `embed_umap.py incremental --batch` subprocess
   — one model load, one batch encode, one write, regardless of how many
   profiles are pending (a 100-person burst costs one model load, not 100). It
   never runs during an attendee request. Publish only after the complete new
   `umap_positions.json` exists, so visitors always see either the previous
   complete match set or the next complete match set—never a partial result.

This deliberately favors a stable "conference structure" over globally fresh
matches: positions are frozen once placed. `incremental` projects a new/edited
profile into the *already-fitted* UMAP space via `.transform()` — their dot
appears (or moves, for an edit) without shifting anyone else's. The tradeoff:
an edit's own top-5 matches are recomputed against everyone else, but it does
**not** retroactively update other people's precomputed top-matches lists —
only a manual `full` run does that (see below). A brand-new signup is folded
into the growing embedding matrix by the same `incremental` call, so they
become a candidate for others' "show more matches" (`/api/matches`, computed
live from that matrix) immediately, even though their precomputed top-5 slot
in other people's lists is unaffected.

`embed_umap.py full` (re-embed everyone, refit UMAP, recompute top-matches for
everyone) still exists but is no longer triggered automatically by profile
edits — it moves every dot, which is exactly what the frozen-position design
avoids. Trigger it manually only for a deliberate structural reset (e.g. the
attendee pool has grown enough that the fitted space is stale), via
`POST /api/admin/rebuild` with `X-Rebuild-Token`, or `python3 embed_umap.py
full` directly.

Operational defaults:

- New/edited profiles are usable immediately; the edited/new person sees their
  own fresh position and matches as soon as the queued job runs (up to the
  3-minute quiet window), without waiting on or triggering a full rebuild.
- A new profile receives a generated profile ID from the API. The browser keeps
  that ID locally; the queued incremental job materializes it into the CSV
  (via `materialize_db_edits()`) and uses it as the stable attendee/UMAP ID.
- Check `GET /api/rebuild` from monitoring. It reports `queued`, `running`,
  `complete`, or `failed` plus timestamps; `detail` holds the pending-profiles
  list while queued/running, and a short summary or failure detail once done.
- The service automatically starts the worker when the queued job has been
  quiet for 3 minutes. Additional edits coalesce into the queued job's
  pending list; an edit arriving during a running rebuild creates one
  follow-up queued job.
- Keep one worker only. A second trigger while a build is running returns the
  existing job instead of consuming more RAM/CPU.

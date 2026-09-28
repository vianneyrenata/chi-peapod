#!/usr/bin/env python3
"""
Embed HFES attendee profiles, project them to 2D for the UMAP scatter, and
precompute each person's top-5 most similar attendees ("top matches").

There are two independent embedding SPACES, each with its own files:
  main  Everyone's academic bio (s_profile / Interest Profile) — umap_positions.json etc.
  side  Optional non-academic "side interests" text (hobbies, quirky stuff) — only people
        who actually filled this in get embedded/placed — side_umap_positions.json etc.
Both spaces go through the same full/incremental machinery below, parametrized by a
Space namedtuple, rather than two copies of this file.

Two modes:
  full         Re-embed everyone from authors.csv, refit UMAP, and
               recompute top-matches for everyone from scratch — overwrites
               umap_positions.json (and side_umap_positions.json, for whoever has
               side-interest text). A full UMAP refit can shift everyone's
               position, not just the person who changed, so server.py no
               longer triggers this automatically — it's a deliberate,
               manual/admin action (POST /api/admin/rebuild, or this command).
  incremental  Embed one or more new/edited profiles, project each into the
               existing (already-fitted) UMAP space via .transform(), and
               compute each one's OWN top-5 matches against the existing
               embedding matrix — cheap, does not move anyone else's dot, and
               does not update anyone else's top-matches list (that needs the
               next `full` run). --batch reads a whole JSON list from stdin and
               places them all with one model load (see server.py's worker) —
               use that for more than a couple of profiles at once; the
               model-load cost is paid once per process, not once per profile.
               The side space is placed too, for whoever in the batch has
               non-blank side-interest text — silently skipped per-person
               otherwise, and skipped entirely (not an error) if the side space
               hasn't been fitted yet (nobody's opted in at the last `full` run).

Output (umap_positions.json) is plain static JSON — a static HTML/JS page can
read it directly with zero server and zero runtime embedding math. The only
thing that genuinely requires re-running this script (not doable in pure
client-side JS) is generating a NEW embedding for changed text.

Usage:
  python3 embed_umap.py full
  python3 embed_umap.py incremental --person-id 642353 --name "Soumyadipta Acharya" --text "..."
  echo '[{"person_id":"1","name":"A","text":"...","side_text":"fountain pens"}]' \
    | python3 embed_umap.py incremental --batch
"""
import argparse
import csv
import io
import json
import os
import sys
import tempfile
from collections import namedtuple
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

MODEL_NAME = "all-MiniLM-L6-v2"
TOP_K = 20
# A UMAP fit needs at least a couple of points to mean anything; below this the side space
# just isn't fitted yet — ponytail: hardcoded floor, not tuned, raise it if a 2-3 person fit
# ever looks too noisy in practice.
MIN_SPACE_PEOPLE = 2
DATA_DIR = Path(__file__).parent
CSV_PATH = DATA_DIR / "authors.csv"
POSITIONS_PATH = DATA_DIR / "umap_positions.json"
EMBEDDINGS_PATH = DATA_DIR / "embeddings.npy"
IDS_PATH = DATA_DIR / "embeddings_person_ids.json"
REDUCER_PATH = DATA_DIR / "umap_reducer.joblib"
SIDE_POSITIONS_PATH = DATA_DIR / "side_umap_positions.json"
SIDE_EMBEDDINGS_PATH = DATA_DIR / "side_embeddings.npy"
SIDE_IDS_PATH = DATA_DIR / "side_embeddings_person_ids.json"
SIDE_REDUCER_PATH = DATA_DIR / "side_umap_reducer.joblib"
# Written by chi_feed.py: {person_id: [displayed_bio, titles+abstracts]}. Server-side only.
EMBED_TEXT_PATH = DATA_DIR / "embed_text.json"
# Same default/override as server.py's STATE, so a subprocess launched by the
# server (which passes HFES_STATE_DIR through its inherited environment) finds
# the same database.
STATE_DIR = Path(os.environ.get("HFES_STATE_DIR", DATA_DIR / "state"))
# Same libpq connection string as server.py (inherited by subprocesses via the environment).
DATABASE_URL = os.environ.get("HFES_DATABASE_URL", "dbname=hfes")


def _publish(tmp_path, final_path: Path) -> None:
    """os.replace() carries the temp file's mode, not the target's — tempfile
    defaults to 0600, which would silently 403 Nginx on every rebuild (this
    already broke the site once). Force the public-static 0644 every time."""
    os.chmod(tmp_path, 0o644)
    os.replace(tmp_path, final_path)


def _atomic_write_text(path: Path, text: str) -> None:
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="", dir=path.parent, delete=False) as f:
        f.write(text)
        tmp = f.name
    _publish(tmp, path)


def load_profile_rows() -> list[tuple]:
    import psycopg  # imported here so the model/UMAP paths don't need a DB driver to load
    with psycopg.connect(DATABASE_URL) as conn:
        return conn.execute("SELECT profile_id, person_id, name, affiliation, bio, is_student, "
                            "side_interest FROM profiles").fetchall()


def materialize_db_edits(edits: list[tuple] | None = None) -> int:
    """Fold explicit profile edits/new signups out of Postgres into the source CSV.

    server.py stores profile edits only in the database. Without this step, `full`
    would keep re-embedding the stale CSV and silently ignore every edit. The
    CSV stays the single source both this script and the frontend's fetch()
    read, so a materialized edit becomes visible everywhere in one full run.
    An unreachable database raises (the job fails and alerts) rather than silently
    skipping every edit.
    """
    if edits is None:
        edits = load_profile_rows()
    if not edits:
        return 0

    with open(CSV_PATH, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = list(reader)
    if "Is Student" not in fieldnames:
        fieldnames = [*fieldnames, "Is Student"]
    if "Side Interest" not in fieldnames:
        fieldnames = [*fieldnames, "Side Interest"]
    by_person_id = {r["Person ID"]: r for r in rows if r.get("Person ID")}

    for profile_id, person_id, name, affiliation, bio, is_student, side_interest in edits:
        key = person_id or profile_id
        row = by_person_id.get(key)
        if row is None:
            row = {field: "" for field in fieldnames}
            row["Person ID"] = key
            rows.append(row)
            by_person_id[key] = row
        row["Author"] = name
        row["Affiliation"] = affiliation
        # s_profile is the canonical bio field now (see bio_text() above) — a self-typed edit
        # has no "**Headline**" line, and that's fine, renderBio() just shows it as plain text.
        row["s_profile"] = bio
        # Internal-only tag (not surfaced anywhere in the UI as a label) driving the frontend's
        # Student View filter; "1"/"" rather than True/False since this round-trips through CSV text.
        row["Is Student"] = "1" if is_student else ""
        # Optional non-academic "side interests" text (hobbies etc.) — the second, separate
        # embedding space below. Blank is the common case; blank means "not embedded there".
        row["Side Interest"] = side_interest or ""

    sio = io.StringIO()
    writer = csv.DictWriter(sio, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)
    _atomic_write_text(CSV_PATH, sio.getvalue())
    print(f"Materialized {len(edits)} DB profile edit(s) into {CSV_PATH.name}.", file=sys.stderr)
    return len(edits)


def load_model():
    from sentence_transformers import SentenceTransformer
    return SentenceTransformer(MODEL_NAME)


_feed_cache = (None, {})


def feed_text(person_id, text: str) -> str:
    """A CHI feed person's displayed bio is only their paper titles (abstracts would bloat the
    CSV every browser downloads), so embed titles + abstracts instead — but only while the text
    is still that feed bio. Claiming a profile re-posts the displayed bio unchanged, which must
    not degrade the embedding; a bio the person wrote themselves is embedded as written.
    Re-read when the file changes: the warm worker outlives many feed syncs."""
    global _feed_cache
    try:
        mtime = EMBED_TEXT_PATH.stat().st_mtime
    except OSError:
        return text
    if _feed_cache[0] != mtime:
        _feed_cache = (mtime, json.loads(EMBED_TEXT_PATH.read_text(encoding="utf-8")))
    entry = _feed_cache[1].get(str(person_id))
    same = entry and text.replace("**", "").strip() == entry[0].replace("**", "").strip()
    return entry[1] if same else text


def bio_text(row) -> str:
    """The canonical bio: s_profile (curated "**Headline**\\nBody" text — see index.html's
    renderBio(), which turns ** into real bold) if present, else the older Interest Profile
    column, else presentation titles as a last resort. ** is stripped here since it's markdown
    noise for the embedding model, not for display (display keeps it, in index.html)."""
    text = ((row.get("s_profile") or "").strip() or row["Interest Profile"].strip()
            or row["Presentation Titles"].strip())
    return feed_text(row.get("Person ID"), text).replace("**", "")


def side_text(row) -> str:
    """The optional non-academic 'side interests' text. Blank for most people — those rows
    are filtered out of the side space entirely by read_profiles(), not embedded at all."""
    return (row.get("Side Interest") or "").strip()


# A Space bundles everything that differs between the two embedding spaces: where its files
# live, and how to pull its text out of a CSV row. full/incremental below loop over both
# spaces instead of two copies of this module.
# `required`: MAIN must succeed for a full/incremental run to be considered successful at all —
# a failure there really does mean the job failed. SIDE is best-effort: a UMAP fit can fail in
# ways unrelated to real bugs at very small N (e.g. the spectral-init eigensolver used by UMAP's
# default init needs a minimum number of points beyond just "more than 1" — confirmed locally: 3
# points raised inside umap/scipy, unrelated to anything in this file), and it's expected to grow
# from zero as people opt in — that must never fail a rebuild that otherwise succeeded for everyone.
Space = namedtuple("Space", "name positions_path embeddings_path ids_path reducer_path text_fn required")
MAIN_SPACE = Space("main", POSITIONS_PATH, EMBEDDINGS_PATH, IDS_PATH, REDUCER_PATH, bio_text, True)
SIDE_SPACE = Space("side interest", SIDE_POSITIONS_PATH, SIDE_EMBEDDINGS_PATH, SIDE_IDS_PATH, SIDE_REDUCER_PATH, side_text, False)


def read_profiles(space: Space = MAIN_SPACE):
    with open(CSV_PATH, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    return [
        {"person_id": row["Person ID"], "name": row["Author"], "text": space.text_fn(row)}
        for row in rows
        if row["Author"] and space.text_fn(row)
    ]


def top_matches_for(i, embeddings, profiles):
    # embeddings are L2-normalized, so a dot product IS the cosine similarity
    sims = embeddings @ embeddings[i]
    order = np.argsort(-sims)
    matches = []
    for j in order:
        if j == i:
            continue
        matches.append({"person_id": profiles[j]["person_id"], "name": profiles[j]["name"], "score": round(float(sims[j]) * 100)})
        if len(matches) == TOP_K:
            break
    return matches


def write_positions(space: Space, people, generated_at):
    # Atomic: Nginx serves this file directly to browsers, and server.py's
    # Catalogue reads it too, both concurrently with this write.
    _atomic_write_text(space.positions_path, json.dumps({
        "model": MODEL_NAME,
        "generated_at": generated_at,  # timestamp of the last FULL refit
        "people": people,              # each entry also carries its own updated_at
    }, indent=2))


def verify_placed(space: Space, person_ids_expected):
    """Defense-in-depth: re-read the just-written files from disk and confirm every person we
    meant to place is actually there, rather than trusting that "no exception was raised" means it
    worked. A real signup once had its rebuild job report "complete" while the profile never
    actually appeared in the public data — nobody, including the person who signed up, was ever
    told. This turns that class of silent failure into an explicit, alertable job failure instead.
    Raises RuntimeError (caught by server.py's worker, which marks the job "failed" with this
    message) rather than returning a bool, so a caller can't accidentally ignore the result."""
    with open(CSV_PATH, encoding="utf-8-sig") as f:
        csv_ids = {row.get("Person ID") for row in csv.DictReader(f)}
    positions_ids = (
        {p["person_id"] for p in json.loads(space.positions_path.read_text()).get("people", [])}
        if space.positions_path.exists() else set()
    )
    missing_csv = sorted(pid for pid in person_ids_expected if pid not in csv_ids)
    missing_positions = sorted(pid for pid in person_ids_expected if pid not in positions_ids)
    if missing_csv or missing_positions:
        raise RuntimeError(
            f"Placement verification failed after write ({space.name} space) — missing from "
            f"{CSV_PATH.name}: {missing_csv or 'none'}; missing from {space.positions_path.name}: "
            f"{missing_positions or 'none'}"
        )


def run_full(spaces=(MAIN_SPACE, SIDE_SPACE), model=None):
    """`spaces` lets a caller restrict the refit to just one space — used for the side-only
    periodic refit (see server.py's periodic_side_refit()): the side space is small and grows
    slowly, so its map benefits from an occasional full refit as more people opt in, but the
    academic space (dense, ~1,650 people from day one, frozen by design) has no reason to move
    on the same cadence — restricting to side-only here is what keeps that map untouched."""
    import umap
    import joblib

    materialize_db_edits()
    # `model` is passed in by the warm worker (see run_worker) so a refit reuses the already-loaded
    # model instead of holding two copies; the cold CLI path still loads it lazily below.
    for space in spaces:
        profiles = read_profiles(space)
        if len(profiles) < MIN_SPACE_PEOPLE:
            print(f"Skipping the {space.name} space refit — only {len(profiles)} "
                  f"{'person has' if len(profiles) == 1 else 'people have'} data for it "
                  f"(need {MIN_SPACE_PEOPLE}+).", file=sys.stderr)
            continue
        print(f"Embedding {len(profiles)} profiles for the {space.name} space with {MODEL_NAME}...", file=sys.stderr)
        if model is None:
            model = load_model()
        try:
            embeddings = model.encode([p["text"] for p in profiles], show_progress_bar=True, normalize_embeddings=True)

            reducer = umap.UMAP(n_components=2, random_state=42)
            coords = reducer.fit_transform(embeddings)

            embeddings_tmp = space.embeddings_path.with_name(space.embeddings_path.stem + ".tmp.npy")
            with open(embeddings_tmp, "wb") as f:
                np.save(f, embeddings)
            _publish(embeddings_tmp, space.embeddings_path)
            _atomic_write_text(space.ids_path, json.dumps([p["person_id"] for p in profiles]))
            reducer_tmp = space.reducer_path.with_name(space.reducer_path.name + ".tmp")
            joblib.dump(reducer, reducer_tmp)
            _publish(reducer_tmp, space.reducer_path)

            print(f"Computing top matches for everyone in the {space.name} space...", file=sys.stderr)
            now = datetime.now(timezone.utc).isoformat()
            people = [
                {"person_id": p["person_id"], "name": p["name"], "x": float(x), "y": float(y),
                 "top_matches": top_matches_for(i, embeddings, profiles), "updated_at": now}
                for i, (p, (x, y)) in enumerate(zip(profiles, coords))
            ]
            write_positions(space, people, now)
            verify_placed(space, [p["person_id"] for p in profiles])
            print(f"Full refit done ({space.name}): {len(people)} people. Wrote {space.positions_path.name}", file=sys.stderr)
        except Exception as exc:
            if space.required:
                raise
            # Best-effort space (the side space): log and move on rather than failing a rebuild
            # that otherwise succeeded for everyone's main profile — see the Space definition above.
            print(f"Skipping the {space.name} space this run — fit failed, will retry next full run: {exc}", file=sys.stderr)


def rank_candidates(all_embeddings, all_ids, person_id, embedding, top_k):
    """Nearest neighbors for `embedding`, excluding person_id's own (possibly stale) row."""
    candidate_idx = [i for i, pid in enumerate(all_ids) if pid != person_id]
    sims = all_embeddings[candidate_idx] @ embedding
    order = np.argsort(-sims)[:top_k]
    return [(all_ids[candidate_idx[j]], float(sims[j])) for j in order]


def upsert_embedding(all_embeddings, all_ids, person_id, embedding):
    """Replace person_id's row if present, else append. Never touches anyone else's row —
    this is what lets incremental grow the match candidate pool without a UMAP refit."""
    if person_id in all_ids:
        all_embeddings = all_embeddings.copy()
        all_embeddings[all_ids.index(person_id)] = embedding
        return all_embeddings, all_ids
    return np.vstack([all_embeddings, embedding]), all_ids + [person_id]


def _place_batch_in_space(space: Space, items, model):
    """Shared body for placing one or more {person_id,name,text} items into `space` via
    .transform() into its already-fitted UMAP — never refits, never moves anyone else's dot.
    space.required=True (the main space) exits the whole process if the space isn't fitted yet,
    since that's a real setup problem, and propagates any placement failure. required=False (the
    side space) just skips quietly on either — it's normal for the side space to not be fitted
    yet early on, and a rare placement hiccup there shouldn't fail an otherwise-fine main placement."""
    if not items:
        return
    if not (space.reducer_path.exists() and space.embeddings_path.exists() and space.ids_path.exists()):
        msg = f"No fitted reducer/embeddings yet for the {space.name} space — run `full` at least once first."
        if space.required:
            print(msg, file=sys.stderr)
            sys.exit(1)
        print(msg + " Skipping for now (bootstraps once enough people opt in).", file=sys.stderr)
        return

    import joblib

    try:
        embeddings = model.encode([p["text"] for p in items], normalize_embeddings=True)

        reducer = joblib.load(space.reducer_path)
        coords = reducer.transform(embeddings)

        all_embeddings = np.load(space.embeddings_path)
        all_ids = json.loads(space.ids_path.read_text())
        positions = json.loads(space.positions_path.read_text()) if space.positions_path.exists() else {"generated_at": None, "people": []}
        people_by_id = {p["person_id"]: p for p in positions["people"]}
        name_by_id = {pid: p["name"] for pid, p in people_by_id.items()}

        now = datetime.now(timezone.utc).isoformat()
        for profile, embedding, (x, y) in zip(items, embeddings, coords):
            person_id = profile["person_id"]
            # Batch-mates already placed earlier in this same loop are visible here too (they're
            # already folded into all_embeddings/all_ids), so people signing up together can match.
            ranked = rank_candidates(all_embeddings, all_ids, person_id, embedding, TOP_K)
            top_matches = [{"person_id": pid, "name": name_by_id.get(pid, "?"), "score": round(score * 100)} for pid, score in ranked]
            all_embeddings, all_ids = upsert_embedding(all_embeddings, all_ids, person_id, embedding)
            name_by_id[person_id] = profile["name"]
            people_by_id[person_id] = {"person_id": person_id, "name": profile["name"], "x": float(x), "y": float(y),
                                        "top_matches": top_matches, "updated_at": now}
            print(f"Placed {profile['name']} at ({x:.2f}, {y:.2f}) in the {space.name} space with "
                  f"{len(top_matches)} top matches, without moving anyone else.", file=sys.stderr)

        # Fold everyone into the growing embedding matrix (replace if an edit, append if new) so
        # future incremental runs — and server.py's live /api/matches — can see them as a match
        # candidate too, without ever refitting UMAP or moving anyone else's dot.
        embeddings_tmp = space.embeddings_path.with_name(space.embeddings_path.stem + ".tmp.npy")
        with open(embeddings_tmp, "wb") as f:
            np.save(f, all_embeddings)
        _publish(embeddings_tmp, space.embeddings_path)
        _atomic_write_text(space.ids_path, json.dumps(all_ids))
        write_positions(space, list(people_by_id.values()), positions.get("generated_at"))
        verify_placed(space, [p["person_id"] for p in items])
    except Exception as exc:
        if space.required:
            raise
        print(f"Skipping the {space.name} space placement this run — failed, will retry next incremental run: {exc}", file=sys.stderr)


def run_incremental_batch(profiles, model=None):
    """Place one or more new/edited profiles in a SINGLE process: one model load, one
    batch encode per space, one write per space — instead of paying the ~1-3s model-load
    cost per profile. This is what makes a burst of signups (e.g. right after a session
    break) cheap: N profiles cost one model load + N tiny encodes, not N model loads.

    profiles: list of {"person_id", "name", "text", "side_text"} dicts ("side_text" is
    optional and usually blank). A person_id appearing twice in the same batch is fine —
    the later entry simply wins (matches upsert_embedding).
    """
    if not profiles:
        return

    # Bring the CSV in sync with these (and any other pending) SQLite edits so everyone
    # shows up in server.py's Catalogue right away, without a UMAP refit.
    materialize_db_edits()

    if model is None:
        model = load_model()

    main_items = [{"person_id": p["person_id"], "name": p["name"], "text": feed_text(p["person_id"], p["text"])}
                  for p in profiles]
    _place_batch_in_space(MAIN_SPACE, main_items, model)

    side_items = [{"person_id": p["person_id"], "name": p["name"], "text": p["side_text"]}
                  for p in profiles if p.get("side_text", "").strip()]
    _place_batch_in_space(SIDE_SPACE, side_items, model)


def run_worker(socket_path: str):
    """Long-lived WARM worker: load the sentence model once, JIT-compile UMAP's transform once, then
    serve jobs over a Unix socket forever. Measured on the production t3.small, a cold `incremental`
    subprocess spends ~20 s of its ~30 s on `import umap` (11 s) + numba JIT of the first transform
    (8 s) + model load, and ~20 ms on the actual work — this process pays that start-up once, so a
    placement takes about a second end to end and a burst of signups clears in seconds, not minutes.
    Protocol: one newline-terminated JSON request per connection, {"kind": "incremental"|"side_only"|
    "full", "items": [...]} → one newline-terminated {"ok": true} or {"ok": false, "error": "..."}.
    server.py (`_run_via_worker`) is the only client; it falls back to the cold subprocess whenever
    this socket is missing or dies mid-job, so this process is an accelerator, never a single point
    of failure. Runs as the `hfes-match-worker` systemd unit (deploy/hfes-match-worker.service)."""
    import socket
    import traceback
    import joblib

    model = load_model()
    model.encode(["warm-up"], normalize_embeddings=True)
    if MAIN_SPACE.reducer_path.exists() and MAIN_SPACE.embeddings_path.exists():
        # One throwaway transform so the ~8 s numba compile happens now, not on the first real signup.
        joblib.load(MAIN_SPACE.reducer_path).transform(np.load(MAIN_SPACE.embeddings_path)[:1])

    path = Path(socket_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(path))
    os.chmod(path, 0o660)
    srv.listen(1)
    print(f"worker ready on {path}", file=sys.stderr, flush=True)
    while True:
        conn, _ = srv.accept()
        with conn:
            data = b""
            while not data.endswith(b"\n"):
                chunk = conn.recv(65536)
                if not chunk:
                    break
                data += chunk
            try:
                req = json.loads(data or "{}")
                kind = req.get("kind")
                if kind == "incremental":
                    run_incremental_batch(req.get("items") or [], model=model)
                elif kind == "side_only":
                    run_full(spaces=(SIDE_SPACE,), model=model)
                elif kind == "full":
                    run_full(model=model)
                else:
                    raise ValueError(f"unknown job kind {kind!r}")
                resp = {"ok": True}
            except SystemExit as exc:  # _place_batch_in_space exits for a missing MAIN reducer — don't let it kill the worker
                resp = {"ok": False, "error": f"job exited with status {exc.code}"}
            except Exception:
                resp = {"ok": False, "error": traceback.format_exc()[-3000:]}
            try:
                conn.sendall((json.dumps(resp) + "\n").encode())
            except OSError:
                pass


def run_incremental(person_id, name, text, side_text=""):
    run_incremental_batch([{"person_id": person_id, "name": name, "text": text, "side_text": side_text}])


def selftest():
    """Assert-based check for materialize_db_edits: existing-row overwrite,
    new-row insert, and no-op when the DB has no profiles table populated."""
    global CSV_PATH
    orig_csv = CSV_PATH
    tmp = Path(tempfile.mkdtemp())
    try:
        CSV_PATH = tmp / "authors.csv"
        fieldnames = ["Author", "Affiliation", "Interest Profile", "s_profile", "Presentation Titles", "Person ID"]
        with open(CSV_PATH, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            w.writerow({"Author": "Old Name", "Affiliation": "Old U", "Interest Profile": "old bio",
                        "s_profile": "", "Presentation Titles": "", "Person ID": "P1"})

        edits = [("P1", "P1", "New Name", "New U", "new bio", 1, "fountain pens"),
                 ("freshtok", None, "Brand New", "Fresh U", "fresh bio", 0, "")]

        n = materialize_db_edits(edits)
        assert n == 2, f"expected 2 edits applied, got {n}"
        mode = CSV_PATH.stat().st_mode & 0o777
        assert mode == 0o644, f"materialize must publish world-readable (Nginx needs this) — got {oct(mode)}"

        with open(CSV_PATH, encoding="utf-8-sig", newline="") as f:
            rows = {r["Person ID"]: r for r in csv.DictReader(f)}
        assert rows["P1"]["Author"] == "New Name", "existing row should be overwritten by the DB edit"
        assert rows["P1"]["s_profile"] == "new bio", "a profile edit should land in s_profile, the canonical bio field"
        assert rows["P1"]["Is Student"] == "1", "is_student=1 in the DB should land as 'Is Student'='1' in the CSV"
        assert rows["P1"]["Side Interest"] == "fountain pens", "side_interest in the DB should land in the CSV's Side Interest column"
        assert "freshtok" in rows, "brand-new DB-only profile should be appended as a new row"
        assert rows["freshtok"]["Author"] == "Brand New"
        assert rows["freshtok"]["Is Student"] == "", "is_student=0 should land as an empty string, not '0'"
        assert rows["freshtok"]["Side Interest"] == "", "blank side_interest should land as an empty string"

        n_again = materialize_db_edits(edits)
        assert n_again == 2, "materializing twice should be idempotent, not duplicate rows"
        with open(CSV_PATH, encoding="utf-8-sig", newline="") as f:
            assert len(list(csv.DictReader(f))) == 2, "re-running must not append duplicate rows"

        # upsert_embedding: append when new, replace-in-place (not grow) when already present
        ids = ["A", "B"]
        embs = np.array([[1.0, 0.0], [0.0, 1.0]])
        embs2, ids2 = upsert_embedding(embs, ids, "C", np.array([1.0, 1.0]))
        assert ids2 == ["A", "B", "C"] and embs2.shape == (3, 2), "new person_id should append a row"
        embs3, ids3 = upsert_embedding(embs2, ids2, "B", np.array([5.0, 5.0]))
        assert ids3 == ["A", "B", "C"] and embs3.shape == (3, 2), "existing person_id should replace in place, not grow"
        assert list(embs3[1]) == [5.0, 5.0], "replaced row should carry the new embedding"
        assert list(embs2[1]) == [0.0, 1.0], "upsert must not mutate the input array (server keeps the old one live until publish)"

        # rank_candidates: excludes the querying person_id's own (possibly stale) row
        ranked = rank_candidates(embs2, ids2, "B", np.array([0.0, 1.0]), top_k=5)
        assert [pid for pid, _ in ranked] == ["C", "A"], "person_id must not match against their own row, and must rank by similarity"

        # bio_text: prefers s_profile (stripped of ** for the embedding text) over Interest
        # Profile, falls back through the chain when fields are blank
        assert bio_text({"s_profile": "**Headline**\nBody", "Interest Profile": "old", "Presentation Titles": ""}) == "Headline\nBody"
        assert bio_text({"s_profile": "", "Interest Profile": "old bio", "Presentation Titles": ""}) == "old bio"
        assert bio_text({"s_profile": "", "Interest Profile": "", "Presentation Titles": "A Talk"}) == "A Talk"

        # side_text: blank when the column's absent/blank, present when it's not
        assert side_text({"Side Interest": "mechanical keyboards"}) == "mechanical keyboards"
        assert side_text({"Side Interest": "  "}) == ""
        assert side_text({}) == ""

        # read_profiles(SIDE_SPACE) must skip anyone with no side-interest text, unlike the
        # main space (which nearly always has SOME bio_text fallback)
        with open(CSV_PATH, encoding="utf-8-sig", newline="") as f:
            all_rows = list(csv.DictReader(f))
        assert len(all_rows) == 2, "sanity: two rows on file"
        side_profiles = read_profiles(SIDE_SPACE)
        assert [p["person_id"] for p in side_profiles] == ["P1"], "only the row with real Side Interest text should enter the side space"
        main_profiles = read_profiles(MAIN_SPACE)
        assert {p["person_id"] for p in main_profiles} == {"P1", "freshtok"}, "everyone with a bio (nearly everyone) stays in the main space"

        # verify_placed: catches exactly the silent-failure class seen in production (a rebuild
        # job reporting "complete" even though the placed profile never made it into the public
        # files) by re-reading both files from disk rather than trusting in-memory bookkeeping.
        # Now takes an explicit `space`, since the same check runs for both spaces.
        test_space = Space("test", tmp / "umap_positions.json", tmp / "x.npy", tmp / "x.json", tmp / "x.joblib", bio_text, True)
        test_space.positions_path.write_text(json.dumps({"people": [{"person_id": "P1", "name": "New Name"}]}))
        verify_placed(test_space, ["P1"])  # present in both CSV (row P1 above) and positions.json — no raise
        try:
            verify_placed(test_space, ["ghost-id"])
            raise AssertionError("verify_placed should have raised for a person_id in neither file")
        except RuntimeError as exc:
            assert "ghost-id" in str(exc), f"error should name the missing id, got: {exc}"

        # verify_placed against a space whose positions file doesn't exist yet at all (the side
        # space, before its first successful full run) must raise rather than crash on a missing file.
        never_fitted_space = Space("never fitted", tmp / "nope.json", tmp / "x2.npy", tmp / "x2.json", tmp / "x2.joblib", bio_text, True)
        try:
            verify_placed(never_fitted_space, ["P1"])
            raise AssertionError("verify_placed should raise when the space was never fitted at all")
        except RuntimeError as exc:
            assert "P1" in str(exc)

        print("selftest OK", file=sys.stderr)
    finally:
        CSV_PATH = orig_csv
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="mode", required=True)
    full_p = sub.add_parser("full", help="Re-embed everyone, refit UMAP, recompute top matches from scratch")
    full_p.add_argument("--side-only", action="store_true",
        help="Only refit the side-interest space — leaves the academic map/positions untouched. "
             "Used for the periodic Hobby-space refit (see server.py's periodic_side_refit()).")
    inc = sub.add_parser("incremental", help="Project + match one or more new/edited profiles against the existing embeddings")
    inc.add_argument("--person-id")
    inc.add_argument("--name")
    inc.add_argument("--text", help="The profile bio/interest text to embed")
    inc.add_argument("--side-text", default="", help="Optional non-academic/hobby text for the side-interest space")
    inc.add_argument("--batch", action="store_true",
        help="Read a JSON array of {person_id,name,text,side_text} objects from stdin and place "
             "them all in one model load, instead of a single profile from --person-id/--name/--text")
    sub.add_parser("selftest", help="Assert-based check of the DB-to-CSV materialize step (no model load)")
    wk = sub.add_parser("worker", help="Long-lived warm worker: model + UMAP loaded once, jobs served over a Unix socket (see run_worker)")
    wk.add_argument("--socket", default=str(DATA_DIR / "state" / "worker.sock"))
    args = parser.parse_args()

    if args.mode == "full":
        run_full(spaces=(SIDE_SPACE,) if args.side_only else (MAIN_SPACE, SIDE_SPACE))
    elif args.mode == "selftest":
        selftest()
    elif args.mode == "worker":
        run_worker(args.socket)
    elif args.batch:
        run_incremental_batch(json.load(sys.stdin))
    else:
        if not (args.person_id and args.name and args.text):
            parser.error("--person-id, --name, and --text are required unless --batch is given")
        run_incremental(args.person_id, args.name, args.text, args.side_text)

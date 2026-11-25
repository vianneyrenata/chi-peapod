#!/usr/bin/env python3
"""Embed attendee bios, project them to 2D with UMAP, and bake each person's top matches.

Two spaces, each with its own files: main (academic bio, umap_positions.json) and side
(opt-in side-interest text, side_umap_positions.json). `full` refits everything and can move
every dot, so it is a deliberate action. `incremental` transforms new/edited profiles into the
fitted space without moving anyone else (their own matches are updated; others' wait for `full`).

Usage:
  python3 embed_umap.py full
  python3 embed_umap.py incremental --person-id 642353 --name "Soumyadipta Acharya" --text "..."
  echo '[{"person_id":"1","name":"A","text":"...","side_text":"fountain pens"}]' \
    | python3 embed_umap.py incremental --batch
"""
import argparse
import csv
import io
import re
import json
import os
import re
import sys
import tempfile
from collections import namedtuple
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

MODEL_NAME = "all-MiniLM-L6-v2"
TOP_K = 20
# Floor for fitting the side space; UMAP needs more than a couple of points.
MIN_SPACE_PEOPLE = 2
DATA_DIR = Path(__file__).parent
CSV_PATH = DATA_DIR / "authors.csv"
POSITIONS_PATH = DATA_DIR / "umap_positions.json"
EMBEDDINGS_PATH = DATA_DIR / "embeddings.npy"
IDS_PATH = DATA_DIR / "embeddings_person_ids.json"
REDUCER_PATH = DATA_DIR / "umap_reducer.joblib"
SIDE_POSITIONS_PATH = DATA_DIR / "side_umap_positions.json"
HOBBY_SIM_PATH = DATA_DIR / "hobby_sim.json"  # per-term hobby similarities for the Hobby view's soft matching
SIDE_EMBEDDINGS_PATH = DATA_DIR / "side_embeddings.npy"
SIDE_IDS_PATH = DATA_DIR / "side_embeddings_person_ids.json"
SIDE_REDUCER_PATH = DATA_DIR / "side_umap_reducer.joblib"
# Written by chi_feed.py: {person_id: [displayed_bio, titles+abstracts]}. Server-side only.
EMBED_TEXT_PATH = DATA_DIR / "embed_text.json"
# Same default/override as server.py's STATE, so a subprocess shares the database.
STATE_DIR = Path(os.environ.get("HFES_STATE_DIR", DATA_DIR / "state"))
# Same libpq connection string as server.py (inherited by subprocesses via the environment).
DATABASE_URL = os.environ.get("HFES_DATABASE_URL", "dbname=hfes")


def _publish(tmp_path, final_path: Path) -> None:
    """tempfile creates 0600; nginx needs 0644 on the published files."""
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
    """Fold profile edits/new signups from Postgres into the CSV so `full` embeds them.
    An unreachable database raises; edits must never be skipped silently."""
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
    if "Claimed" not in fieldnames:
        fieldnames = [*fieldnames, "Claimed"]
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
        row["s_profile"] = bio
        # "1"/"" because this round-trips through CSV text.
        row["Is Student"] = "1" if is_student else ""
        row["Side Interest"] = side_interest or ""
        # Any DB row means the person claimed the profile, so they're attending (circle after the name).
        row["Claimed"] = "1"

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
    """Feed people's displayed bio is only their titles; embed titles + abstracts instead, but only while
    the text still equals the feed bio (a claim re-posts it unchanged; a self-written bio is embedded as
    written). Re-read when the file changes: the warm worker outlives many feed syncs."""
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
    """s_profile, else Interest Profile, else titles. ** is markdown for display, noise for the model."""
    text = ((row.get("s_profile") or "").strip() or (row.get("Interest Profile") or "").strip()
            or (row.get("Presentation Titles") or "").strip())
    return feed_text(row.get("Person ID"), text).replace("**", "")


def side_text(row) -> str:
    """Optional side-interest text; blank rows are left out of the side space."""
    return (row.get("Side Interest") or "").strip()


# MAIN must succeed; SIDE is best-effort (UMAP can fail at very small N, and the space grows from zero).
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


def coauthor_index(rows) -> dict[str, set[str]]:
    """People sharing a presentation (authorship roles only, not chairing). Dropped from academic top
    matches: they share paper text and would fill everyone's list. server.py and index.html mirror this."""
    title_people: dict[str, set[str]] = {}
    for r in rows:
        if not (r.get("Author") and r.get("Person ID")):
            continue
        for part in (r.get("Schedule Detail") or "").split("|"):
            part = part.strip()
            role = re.search(r"\[([^\]]+)\]$", part)
            if not part or (role and role.group(1) == "Session Chair"):
                continue
            title = (part[:role.start()] if role else part).rsplit("·", 1)[-1].strip().lower()
            key = f"{title}|{part.split(' ', 1)[0]}"  # date prefix disambiguates generic titles
            title_people.setdefault(key, set()).add(r["Person ID"])
        # Unscheduled papers have no Schedule Detail; group those by title alone (same-title papers would merge).
        if not (r.get("Schedule Detail") or "").strip():
            for title in (r.get("Presentation Titles") or "").split("; "):
                if title.strip():
                    title_people.setdefault(f"{title.strip().lower()}|unscheduled", set()).add(r["Person ID"])
    coauthors: dict[str, set[str]] = {}
    for ids in title_people.values():
        if len(ids) > 1:
            for pid in ids:
                coauthors.setdefault(pid, set()).update(ids - {pid})
    return coauthors


def read_coauthors() -> dict[str, set[str]]:
    with open(CSV_PATH, encoding="utf-8-sig") as f:
        return coauthor_index(csv.DictReader(f))


def top_matches_for(i, embeddings, profiles, exclude=frozenset()):
    # embeddings are L2-normalized, so a dot product IS the cosine similarity
    sims = embeddings @ embeddings[i]
    order = np.argsort(-sims)
    matches = []
    for j in order:
        if j == i or profiles[j]["person_id"] in exclude:
            continue
        matches.append({"person_id": profiles[j]["person_id"], "name": profiles[j]["name"], "score": round(float(sims[j]) * 100)})
        if len(matches) == TOP_K:
            break
    return matches


def write_positions(space: Space, people, generated_at):
    # Atomic: nginx and server.py read this file while it's being written.
    _atomic_write_text(space.positions_path, json.dumps({
        "model": MODEL_NAME,
        "generated_at": generated_at,  # time of the last change to this file (full refit OR incremental placement)
        "people": people,              # each entry also carries its own updated_at
    }, indent=2))


def verify_placed(space: Space, person_ids_expected):
    """Re-read the written files and confirm every expected person is there. Raises RuntimeError so
    the job is marked failed instead of silently reporting success."""
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
    """`spaces` restricts the refit: the periodic side-only refit must not move the frozen academic map."""
    import umap
    import joblib

    materialize_db_edits()
    # `model` comes from the warm worker so a refit reuses the loaded model.
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
            # Co-authors of one paper share the same text (~56% of people): embed each distinct text once.
            # index.html nudges the stacked dots apart.
            texts = [p["text"] for p in profiles]
            uniq = list(dict.fromkeys(texts))
            row_of = {t: k for k, t in enumerate(uniq)}
            idx = np.array([row_of[t] for t in texts])
            print(f"{len(uniq)} distinct texts among {len(texts)} profiles.", file=sys.stderr)
            uniq_embeddings = model.encode(uniq, show_progress_bar=True, normalize_embeddings=True)
            embeddings = uniq_embeddings[idx]

            reducer = umap.UMAP(n_components=2, random_state=42)
            coords = reducer.fit_transform(uniq_embeddings)[idx]

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
            coauthors = read_coauthors() if space is MAIN_SPACE else {}
            people = [
                {"person_id": p["person_id"], "name": p["name"], "x": float(x), "y": float(y),
                 "top_matches": top_matches_for(i, embeddings, profiles, coauthors.get(p["person_id"], frozenset())),
                 "updated_at": now}
                for i, (p, (x, y)) in enumerate(zip(profiles, coords))
            ]
            write_positions(space, people, now)
            verify_placed(space, [p["person_id"] for p in profiles])
            print(f"Full refit done ({space.name}): {len(people)} people. Wrote {space.positions_path.name}", file=sys.stderr)
        except Exception as exc:
            if space.required:
                raise
            # Side space is best-effort: log and keep the main result.
            print(f"Skipping the {space.name} space this run — fit failed, will retry next full run: {exc}", file=sys.stderr)
    if model is not None:
        write_hobby_sim(model)


def rank_candidates(all_embeddings, all_ids, person_id, embedding, top_k, exclude=frozenset()):
    """Nearest neighbors for `embedding`, excluding person_id's own (possibly stale) row and `exclude`."""
    candidate_idx = [i for i, pid in enumerate(all_ids) if pid != person_id and pid not in exclude]
    sims = all_embeddings[candidate_idx] @ embedding
    order = np.argsort(-sims)[:top_k]
    return [(all_ids[candidate_idx[j]], float(sims[j])) for j in order]


def upsert_embedding(all_embeddings, all_ids, person_id, embedding):
    """Replace person_id's row if present, else append; never touches other rows."""
    if person_id in all_ids:
        all_embeddings = all_embeddings.copy()
        all_embeddings[all_ids.index(person_id)] = embedding
        return all_embeddings, all_ids
    return np.vstack([all_embeddings, embedding]), all_ids + [person_id]



# ---- Hobby term similarity (soft matching in the Hobby view) -------------------------------
# index.html's parseHobbies() ported 1:1 — the keys here must equal the keys the browser derives,
# or a person's hobby won't find its row in hobby_sim.json and silently falls back to exact match.
_HOBBY_SPLIT = re.compile(r"[,;\n/]|\band\b|&")
HOBBY_SIM_MIN = 0.35  # pairs below this aren't worth shipping; the browser's credit curve (HOBBY_FLOOR) is 0 there anyway


def parse_hobbies(text: str) -> list[str]:
    out = []
    for t in _HOBBY_SPLIT.split((text or "").lower()):
        t = re.sub(r"\s+", " ", re.sub(r"[.!]+$", "", t)).strip()
        if not t or len(t) > 40:
            continue
        if len(t) > 4 and re.search(r"[^s]s$", t) and not re.search(r"(ss|us|is)$", t):
            t = t[:-1]  # plain plurals → singular
        if t not in out:
            out.append(t)
    return out


def hobby_terms_from_csv(path: Path = None) -> list[str]:
    with open(path or CSV_PATH, newline="", encoding="utf-8") as f:
        terms = []
        for row in csv.DictReader(f):
            for t in parse_hobbies(row.get("Side Interest") or ""):
                if t not in terms:
                    terms.append(t)
    return sorted(terms)


def hobby_sim_payload(terms: list[str], model) -> dict:
    """{terms, pairs:[[i, j, sim], ...]} with i<j and sim >= HOBBY_SIM_MIN. A few hundred terms at
    most, so this is one small encode; stored sparse so the browser download stays a few KB."""
    pairs = []
    if len(terms) >= 2:
        emb = model.encode(terms, normalize_embeddings=True)
        sims = emb @ emb.T
        for i in range(len(terms)):
            for j in range(i + 1, len(terms)):
                s = float(sims[i, j])
                if s >= HOBBY_SIM_MIN:
                    pairs.append([i, j, round(s, 3)])
    return {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "terms": terms, "pairs": pairs}


def write_hobby_sim(model) -> int:
    """Embed every distinct hobby term in the CSV and publish hobby_sim.json. Called after every
    placement that carried hobby text and after every full run. Never fatal: the Hobby view
    works exact-match-only without the file."""
    try:
        terms = hobby_terms_from_csv()
        payload = hobby_sim_payload(terms, model)
        _atomic_write_text(HOBBY_SIM_PATH, json.dumps(payload, ensure_ascii=False))
        print(f"Wrote {HOBBY_SIM_PATH.name}: {len(terms)} hobby term(s), {len(payload['pairs'])} pair(s) >= {HOBBY_SIM_MIN}.", file=sys.stderr)
        return len(terms)
    except Exception as e:  # noqa: BLE001 — best-effort by design, same stance as the side space
        print(f"hobby_sim: skipped ({e})", file=sys.stderr)
        return -1


def _place_batch_in_space(space: Space, items, model):
    """Place items into `space` via .transform() on the fitted UMAP: never refits, never moves others.
    A required space exits on a missing fit and propagates failures; the side space skips quietly."""
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
        coauthors = read_coauthors() if space is MAIN_SPACE else {}
        for profile, embedding, (x, y) in zip(items, embeddings, coords):
            person_id = profile["person_id"]
            # Batch-mates placed earlier in this loop are already candidates.
            ranked = rank_candidates(all_embeddings, all_ids, person_id, embedding, TOP_K,
                                     coauthors.get(person_id, frozenset()))
            top_matches = [{"person_id": pid, "name": name_by_id.get(pid, "?"), "score": round(score * 100)} for pid, score in ranked]
            all_embeddings, all_ids = upsert_embedding(all_embeddings, all_ids, person_id, embedding)
            name_by_id[person_id] = profile["name"]
            people_by_id[person_id] = {"person_id": person_id, "name": profile["name"], "x": float(x), "y": float(y),
                                        "top_matches": top_matches, "updated_at": now}
            print(f"Placed {profile['name']} at ({x:.2f}, {y:.2f}) in the {space.name} space with "
                  f"{len(top_matches)} top matches, without moving anyone else.", file=sys.stderr)

        # Grow the embedding matrix so later placements and /api/matches see them, without a refit.
        embeddings_tmp = space.embeddings_path.with_name(space.embeddings_path.stem + ".tmp.npy")
        with open(embeddings_tmp, "wb") as f:
            np.save(f, all_embeddings)
        _publish(embeddings_tmp, space.embeddings_path)
        _atomic_write_text(space.ids_path, json.dumps(all_ids))
        # Stamp with now, not the previous value: the browser's "last updated" label and the "a newer
        # map is ready" nudge both read this, and a new person on the map IS a change (owner's call 2026-10-16).
        write_positions(space, list(people_by_id.values()), now)
        verify_placed(space, [p["person_id"] for p in items])
    except Exception as exc:
        if space.required:
            raise
        print(f"Skipping the {space.name} space placement this run — failed, will retry next incremental run: {exc}", file=sys.stderr)


def run_incremental_batch(profiles, model=None):
    """Place one or more profiles in one process: one model load, one encode and one write per space.
    profiles: [{"person_id","name","text","side_text"?}]; for a duplicate person_id the later entry wins."""
    if not profiles:
        return

    # Sync the CSV with pending DB edits so everyone appears in the Catalogue right away.
    materialize_db_edits()

    if model is None:
        model = load_model()

    main_items = [{"person_id": p["person_id"], "name": p["name"], "text": feed_text(p["person_id"], p["text"])}
                  for p in profiles]
    _place_batch_in_space(MAIN_SPACE, main_items, model)

    side_items = [{"person_id": p["person_id"], "name": p["name"], "text": p["side_text"]}
                  for p in profiles if p.get("side_text", "").strip()]
    _place_batch_in_space(SIDE_SPACE, side_items, model)
    if side_items:
        write_hobby_sim(model)


def run_worker(socket_path: str):
    """Warm worker: load the model and JIT UMAP once, then serve jobs over a Unix socket.
    Protocol: one newline-terminated JSON request per connection,
    {"kind": "incremental"|"side_only"|"full", "items": [...]} -> {"ok": true} | {"ok": false, "error": "..."}.
    server.py falls back to a cold subprocess when the socket is down."""
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

        # bio_text: s_profile (** stripped) over Interest Profile, then the fallback chain
        assert bio_text({"s_profile": "**Headline**\nBody", "Interest Profile": "old", "Presentation Titles": ""}) == "Headline\nBody"
        assert bio_text({"s_profile": "", "Interest Profile": "old bio", "Presentation Titles": ""}) == "old bio"
        assert bio_text({"s_profile": "", "Interest Profile": "", "Presentation Titles": "A Talk"}) == "A Talk"

        # side_text: blank when the column's absent/blank, present when it's not
        assert side_text({"Side Interest": "mechanical keyboards"}) == "mechanical keyboards"
        assert side_text({"Side Interest": "  "}) == ""
        assert side_text({}) == ""

        # read_profiles(SIDE_SPACE) skips anyone with no side text
        with open(CSV_PATH, encoding="utf-8-sig", newline="") as f:
            all_rows = list(csv.DictReader(f))
        assert len(all_rows) == 2, "sanity: two rows on file"
        side_profiles = read_profiles(SIDE_SPACE)
        assert [p["person_id"] for p in side_profiles] == ["P1"], "only the row with real Side Interest text should enter the side space"
        main_profiles = read_profiles(MAIN_SPACE)
        assert {p["person_id"] for p in main_profiles} == {"P1", "freshtok"}, "everyone with a bio (nearly everyone) stays in the main space"

        # verify_placed re-reads both files from disk rather than trusting in-memory bookkeeping
        test_space = Space("test", tmp / "umap_positions.json", tmp / "x.npy", tmp / "x.json", tmp / "x.joblib", bio_text, True)
        test_space.positions_path.write_text(json.dumps({"people": [{"person_id": "P1", "name": "New Name"}]}))
        verify_placed(test_space, ["P1"])  # present in both CSV (row P1 above) and positions.json — no raise
        try:
            verify_placed(test_space, ["ghost-id"])
            raise AssertionError("verify_placed should have raised for a person_id in neither file")
        except RuntimeError as exc:
            assert "ghost-id" in str(exc), f"error should name the missing id, got: {exc}"

        # verify_placed on a space with no positions file yet must raise, not crash
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
    sub.add_parser("hobby-sim", help="Re-embed the distinct hobby terms in the CSV and write hobby_sim.json (one model load)")
    wk = sub.add_parser("worker", help="Long-lived warm worker: model + UMAP loaded once, jobs served over a Unix socket (see run_worker)")
    wk.add_argument("--socket", default=str(DATA_DIR / "state" / "worker.sock"))
    args = parser.parse_args()

    if args.mode == "full":
        run_full(spaces=(SIDE_SPACE,) if args.side_only else (MAIN_SPACE, SIDE_SPACE))
    elif args.mode == "hobby-sim":
        write_hobby_sim(load_model())
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

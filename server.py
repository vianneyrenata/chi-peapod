#!/usr/bin/env python3
"""HFE Peapod (formerly HFES Match) application server.

Static pages are deliberately served by Nginx in production.  This process only
handles the small JSON API and keeps the conference catalogue in memory, so a
burst of attendee browsing never touches SQLite or the embedding model unless it
needs to.

Flask app served by gunicorn in production (deploy/gunicorn.conf.py); `python3
server.py` runs Flask's dev server for local work.  Both run ONE process with many
threads: the rebuild timer, REBUILD_LOCK and the per-IP rate-limit windows live in
process memory, so a second worker process would run a second, unsynchronised copy
of all of them.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from http import HTTPStatus
from pathlib import Path

import psycopg
from flask import Flask, jsonify, request
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parent
STATE = Path(os.environ.get("HFES_STATE_DIR", ROOT / "state"))
# libpq connection string. The default reaches a local server over its Unix socket with peer auth:
# the OS user IS the Postgres role, so there is no password to store. Same var in embed_umap.py.
DATABASE_URL = os.environ.get("HFES_DATABASE_URL", "dbname=hfes")
ADMIN_TOKEN = os.environ.get("HFES_REEMBED_TOKEN", "")
MAX_BODY = 64 * 1024
REBUILD_DELAY_SECONDS = max(0, int(os.environ.get("HFES_AUTO_REBUILD_SECONDS", "900")))

# Periodic Hobby-space-only full refit (see periodic_side_refit_check() below). The academic
# space never does this — it's dense and frozen by design — but the Hobby space starts empty and
# grows slowly as people opt in, so its projection is worth reshaping occasionally, unlike a
# per-edit incremental placement which only ever adds/moves that one person's own dot.
SIDE_REFIT_INTERVAL_SECONDS = max(0, int(os.environ.get("HFES_SIDE_REFIT_INTERVAL_SECONDS", str(24 * 60 * 60))))
SIDE_REFIT_CHECK_SECONDS = max(60, int(os.environ.get("HFES_SIDE_REFIT_CHECK_SECONDS", str(60 * 60))))
# CHI programme feed poll (chi_feed.py). The feed asks for once a minute during the conference;
# 0 disables it (tests, or a box that should keep serving a frozen copy).
FEED_CHECK_SECONDS = max(0, int(os.environ.get("HFES_FEED_CHECK_SECONDS", "60")))
SIDE_REFIT_INITIAL_DELAY_SECONDS = max(1, int(os.environ.get("HFES_SIDE_REFIT_INITIAL_DELAY_SECONDS", "60")))

# Optional ops alert on a failed rebuild job — sent via plain SMTP (stdlib smtplib, no new
# dependency) rather than a cloud notification service, since server.py stays dependency-free by
# design. Absent config = quietly does nothing, same pattern as ANTHROPIC_API_KEY below.
ALERT_EMAIL_TO = os.environ.get("HFES_ALERT_EMAIL_TO", "")
ALERT_SMTP_HOST = os.environ.get("HFES_ALERT_SMTP_HOST", "smtp.gmail.com")
ALERT_SMTP_PORT = int(os.environ.get("HFES_ALERT_SMTP_PORT", "587"))
ALERT_SMTP_USER = os.environ.get("HFES_ALERT_SMTP_USER", "")
ALERT_SMTP_PASSWORD = os.environ.get("HFES_ALERT_SMTP_PASSWORD", "")


def send_alert_email(subject: str, body: str) -> None:
    """Best-effort ops alert. Never raises and never blocks the caller — an alert failing to send
    must not crash the rebuild worker or mask the original error. Quietly does nothing if unset."""
    if not (ALERT_EMAIL_TO and ALERT_SMTP_USER and ALERT_SMTP_PASSWORD):
        return
    try:
        import smtplib
        from email.mime.text import MIMEText
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = ALERT_SMTP_USER
        msg["To"] = ALERT_EMAIL_TO
        with smtplib.SMTP(ALERT_SMTP_HOST, ALERT_SMTP_PORT, timeout=15) as smtp:
            smtp.starttls()
            smtp.login(ALERT_SMTP_USER, ALERT_SMTP_PASSWORD)
            smtp.sendmail(ALERT_SMTP_USER, [ALERT_EMAIL_TO], msg.as_string())
    except Exception as exc:
        print(f"send_alert_email: failed to send: {exc}", file=sys.stderr)

# Optional: drafts a short bio for a brand-new signup via a web-search-backed Haiku call.
# Absent key = feature quietly disabled (draft_bio() returns None), never a hard error.
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
DRAFT_BIO_SYSTEM_PROMPT = """You are drafting a short STARTING POINT for a conference attendee's bio —
not a finished biography. They will see their own name directly above this text and are expected to \
edit and add to it themselves; the goal is to give them a spark to react to, not to write the whole \
thing for them. You will be given a name and an affiliation.

Use the web_search tool to find reliable, directly relevant information: prefer the person's own \
personal or faculty page, their university's or company's official directory/profile page, or their \
own publications/lab page. Do not use social-media aggregators, unrelated news mentions, or pages \
about a different person who happens to share the name. Do a small number of targeted searches, not a \
broad scour.

This bio is for an attendee of the HFES (Human Factors and Ergonomics Society) conference. If your \
search turns up more than one real person matching this name and affiliation, prefer whichever one \
works in human factors, ergonomics, or a closely related field (e.g. cognitive engineering, \
human-computer interaction, safety, UX) — that person is far more likely to be the actual attendee. \
Only use this to choose between multiple genuine candidates you actually found; never invent a \
human-factors connection that isn't really there.

If you cannot confidently confirm you found the right person (name and affiliation both line up, or \
there is strong contextual evidence), your entire final response must be exactly the single word \
NO_MATCH — no explanation of your search, no reasoning, nothing else, not even a sentence before it.

Otherwise, output exactly this template, filled in — always both lines, in this order, nothing before \
or after:

**<short punchy headline capturing their research focus, a few words, never their name, never \
starting with the word "The">**
<two to three short sentences, about 50 words total, describing their research area and interests>

The first line is always present and always wrapped in double asterisks — never skip straight to the \
sentences. Do not open the body with their name — it's already shown right above this text, so \
restating it is redundant. You may bold one distinguishing phrase within the body if it helps someone \
scan it in a few seconds — no more than one. Still leave room for them to add to it: this is a \
starting point, not a complete biography, so don't try to cover everything you found.

Do not fabricate anything you could not verify from the sources you found. Your entire final response \
must be only those two lines — no preamble like "I'll search for..." or "Based on the results...", no \
extra paragraphs, no meta-commentary about the search process."""

# Soft per-IP cooldown against a scripted burst driving up Anthropic spend — this endpoint has no auth
# (the whole app doesn't). Not a hard budget cap; set a real spending limit in the Anthropic Console too.
_DRAFT_BIO_LOCK = threading.Lock()
_draft_bio_last_call: dict[str, float] = {}
DRAFT_BIO_COOLDOWN_SECONDS = 20


def client_ip(headers, fallback: str) -> str:
    """The visitor's IP as seen through CloudFront. Nginx's X-Real-IP is $remote_addr, which behind
    CloudFront is the EDGE's address — so any per-IP limit keyed on it is really per-edge (one abuser
    shares a bucket with every real visitor at that edge). CloudFront appends the viewer IP as the LAST
    X-Forwarded-For hop, and the viewer can't forge that hop; take it. Direct-to-origin traffic (no
    CloudFront hop) falls back to X-Real-IP, which is then the real client anyway."""
    xff = headers.get("X-Forwarded-For", "")
    return xff.rsplit(",", 1)[-1].strip() or headers.get("X-Real-IP") or fallback


# POST /api/profile is unauthenticated by design (good-faith claiming) — this is the only thing
# standing between a script and overwriting all ~1,650 bios in seconds. A real person editing their
# own profile makes a handful of writes; cap at 20 per IP per 10 minutes. The browser already
# treats a failed publish as "saved on this device, will retry", so a tripped limit loses nothing.
# ponytail: in-memory sliding window, resets on restart, one list per active IP — fine for a
# one-week event; move to SQLite if the process ever needs to remember across restarts.
_PROFILE_WRITE_LOCK = threading.Lock()
_profile_writes: dict[str, list[float]] = {}
PROFILE_WRITES_PER_WINDOW = 20
PROFILE_WINDOW_SECONDS = 600


def profile_write_allowed(ip: str, now: float) -> bool:
    with _PROFILE_WRITE_LOCK:
        recent = [t for t in _profile_writes.get(ip, []) if now - t < PROFILE_WINDOW_SECONDS]
        if len(recent) >= PROFILE_WRITES_PER_WINDOW:
            _profile_writes[ip] = recent
            return False
        _profile_writes[ip] = recent + [now]
        return True

# The browser calls /api/client-error when it can't load the CSVs/embeddings at all (a real
# outage, not the normal "server just hasn't finished a rebuild yet" case) — reuses the same
# send_alert_email() as a failed rebuild job. A GLOBAL cooldown, not per-IP: a real outage means
# many different visitors' browsers all hit this within seconds of each other, and per-IP
# wouldn't stop that from being one email per visitor.
_CLIENT_ERROR_LOCK = threading.Lock()
_last_client_error_alert = 0.0
CLIENT_ERROR_COOLDOWN_SECONDS = 600


def draft_bio(name: str, aff: str) -> str | None:
    """Ask Haiku (with web search) for a ~100-word bio. None if disabled, no confident match, or any
    failure — callers must treat this as a nice-to-have, never block signup on it."""
    if not ANTHROPIC_API_KEY:
        return None
    body = json.dumps({
        "model": "claude-haiku-4-5",
        "max_tokens": 3000,
        # Without extended thinking, Haiku 4.5 has no private scratchpad — its multi-round search
        # reasoning ("I'll search... not found... let me try again...") lands directly in visible
        # text blocks instead. Confirmed live: this leaked as the "bio" before thinking was added
        # here. budget_tokens must be < max_tokens on this (pre-4.6) model.
        "thinking": {"type": "enabled", "budget_tokens": 1536},
        "system": DRAFT_BIO_SYSTEM_PROMPT,
        "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}],
        "messages": [{"role": "user", "content": f"Name: {name}\nAffiliation: {aff}"}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        print(f"draft_bio: request failed: {exc}", file=sys.stderr)
        return None
    if data.get("stop_reason") == "refusal":
        return None
    # Join every text block that comes AFTER the last non-text block (thinking/tool_use/tool_result),
    # not just the very last block. Confirmed live via a one-off debug dump: the two-line answer
    # (headline, then sentence) is sometimes split across two separate consecutive text blocks — an
    # earlier version took only the last of those and silently dropped the headline. Interim search
    # narration ("I'll search...", "Not found, let me try again...") still gets excluded correctly,
    # since it's separated from the final answer by at least one tool_use/tool_result block.
    content = data.get("content", [])
    last_non_text_idx = max((i for i, b in enumerate(content) if b.get("type") != "text"), default=-1)
    text = "".join(b.get("text", "") for i, b in enumerate(content)
                   if i > last_non_text_idx and b.get("type") == "text").strip()
    # Confirmed live: even the final block can carry a stray leading fragment (e.g. a trailing "."
    # left over from a truncated lead-in sentence) before the real bio starts.
    text = re.sub(r"^[.\s]+", "", text)
    # Confirmed live: sometimes a whole preamble sentence ("Based on my search results, I found
    # that...") precedes the correctly-formatted answer with no tool-call boundary in between for the
    # block-splitting logic above to key off. A real answer always starts with the bolded headline
    # line the prompt requires; ordinary narration never uses that markdown — so if a bolded line
    # appears anywhere, trust it over anything before it rather than the raw block boundaries.
    headline_match = re.search(r"\*\*[^\n*]+\*\*\n.+", text, re.DOTALL)
    if headline_match:
        text = headline_match.group(0).strip()
    # Belt-and-suspenders beyond the prompt: Haiku sometimes narrates its search ("I'll look this
    # up... NO_MATCH") instead of replying with only the sentinel. Treat NO_MATCH appearing anywhere
    # as a decline — the alternative (an exact-match check only) would hand that narration to a real
    # attendee as their "bio," which happened in testing before this check was added.
    if not text or re.search(r"\bNO_MATCH\b", text):
        return None
    return text[:2000]  # defensive cap; the prompt already asks for ~100 words


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles (
  profile_id TEXT PRIMARY KEY, person_id TEXT, name TEXT NOT NULL,
  affiliation TEXT NOT NULL DEFAULT '', bio TEXT NOT NULL DEFAULT '',
  is_student INTEGER NOT NULL DEFAULT 0,
  side_interest TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS profiles_person_id
  ON profiles(person_id) WHERE person_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS jobs (
  job_id TEXT PRIMARY KEY, status TEXT NOT NULL, requested_at TEXT NOT NULL,
  started_at TEXT, finished_at TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS feedback (
  feedback_id TEXT PRIMARY KEY, message TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS profile_edits (
  edit_id TEXT PRIMARY KEY, profile_id TEXT NOT NULL, person_id TEXT,
  change_json TEXT NOT NULL, created_at TEXT NOT NULL
);
"""
# ponytail: timestamps stay ISO-8601 TEXT, exactly as in the SQLite era — the browser compares
# updatedAt strings to detect a placement, and ISO text sorts chronologically. TIMESTAMPTZ would be
# the idiomatic column type; switching means changing that comparison too.


class Store:
    # ponytail: one shared connection serialised by self.lock, like the SQLite version — every query
    # is sub-millisecond. Move to psycopg_pool if DB time ever shows up in request latency.
    def __init__(self) -> None:
        STATE.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self._conn = None
        with self.tx() as db:
            db.execute(SCHEMA)

    @property
    def conn(self):
        # Reconnect after a Postgres restart instead of failing every request from then on;
        # the request that finds the dead connection still errors, the next one gets a new one.
        if self._conn is None or self._conn.closed or self._conn.broken:
            self._conn = psycopg.connect(DATABASE_URL, autocommit=True, row_factory=dict_row)
        return self._conn

    @contextmanager
    def tx(self):
        """One transaction, holding the lock: commits on success, rolls back on any exception."""
        with self.lock:
            conn = self.conn
            with conn.transaction():
                yield conn

    def _one(self, sql: str, values: tuple = ()):
        with self.tx() as db:
            return db.execute(sql, values).fetchone()

    def profile(self, profile_id: str):
        return self._one("SELECT * FROM profiles WHERE profile_id=%s", (profile_id,))

    def upsert_profile(self, data: dict) -> dict:
        profile_id = str(data.get("profileId") or data.get("personId") or secrets.token_urlsafe(18))
        person_id = str(data["personId"]) if data.get("personId") else None
        name = str(data.get("name", "")).strip()[:200]
        affiliation = str(data.get("aff", data.get("affiliation", ""))).strip()[:500]
        bio = str(data.get("bio", "")).strip()[:12000]
        is_student = 1 if data.get("isStudent") else 0
        side_interest = str(data.get("sideInterest", "")).strip()[:4000]
        if not name:
            raise ValueError("Name is required.")
        now = utcnow()
        with self.tx() as db:
            previous = db.execute("SELECT name,affiliation,bio,is_student,side_interest FROM profiles WHERE profile_id=%s", (profile_id,)).fetchone()
            db.execute("""INSERT INTO profiles(profile_id,person_id,name,affiliation,bio,is_student,side_interest,updated_at)
              VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(profile_id) DO UPDATE SET
              person_id=excluded.person_id,name=excluded.name,affiliation=excluded.affiliation,
              bio=excluded.bio,is_student=excluded.is_student,side_interest=excluded.side_interest,updated_at=excluded.updated_at""",
              (profile_id, person_id, name, affiliation, bio, is_student, side_interest, now))
            before = dict(previous) if previous else None
            after = {"name": name, "affiliation": affiliation, "bio": bio, "is_student": is_student, "side_interest": side_interest}
            if before != after:
                db.execute("INSERT INTO profile_edits VALUES(%s,%s,%s,%s,%s)",
                    (secrets.token_urlsafe(12), profile_id, person_id,
                     json.dumps({"before": before, "after": after}, separators=(",", ":")), now))
        return self.profile(profile_id)

    def add_feedback(self, message: str) -> None:
        clean = message.strip()
        if not 3 <= len(clean) <= 2000:
            raise ValueError("Feedback must be between 3 and 2,000 characters.")
        with self.tx() as db:
            db.execute("INSERT INTO feedback VALUES(%s,%s,%s)", (secrets.token_urlsafe(12), clean, utcnow()))

    def current_job(self):
        return self._one("SELECT * FROM jobs ORDER BY requested_at DESC LIMIT 1")


class Catalogue:
    """Read-only conference data, loaded once and refreshed after a full build."""
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.people: dict[str, dict] = {}
        self.generated_at = None
        # Two independent embedding spaces: "main" (academic bio, always populated) and "side"
        # (optional non-academic interests — only whoever's filled it in, may be empty entirely
        # before the first full run that finds enough people). Same shape for both, keyed by
        # name, so matches()/match_page() take a `space` argument instead of duplicating.
        self._spaces: dict[str, dict] = {"main": {}, "side": {}}
        self.reload()

    def reload(self) -> None:
        with open(ROOT / "authors.csv", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        # s_profile is the canonical bio ("**Headline**\nBody", rendered bold client-side by
        # index.html's renderBio()) — same precedence as embed_umap.py's bio_text().
        people = {r["Person ID"]: {"personId": r["Person ID"], "name": r["Author"], "aff": r["Affiliation"],
            "bio": r.get("s_profile") or r["Interest Profile"] or r["Presentation Titles"] or "No profile on file.",
            "isStudent": (r.get("Is Student") or "").strip() == "1",
            "sideInterest": r.get("Side Interest") or ""}
            for r in rows if r.get("Author") and r.get("Person ID")}

        # Co-author index: two people co-authored if any presentation (title + date, from their
        # Schedule Detail entries) appears in both their schedules with an authorship role —
        # Session Chair doesn't count, chairing isn't co-authoring. Used to drop co-authors from
        # ACADEMIC top matches only (they share paper text, so embeddings rank them near the top,
        # but "go meet your own labmate" is noise). Hobby-space matches are never filtered — a
        # shared hobby with a co-author is a genuine match. index.html builds the same index
        # client-side for its offline fallback paths; keep the two derivations in sync.
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
        coauthors: dict[str, set[str]] = {}
        for ids in title_people.values():
            if len(ids) > 1:
                for pid in ids:
                    coauthors.setdefault(pid, set()).update(ids - {pid})

        spaces = {}
        for key, positions_file, embeddings_file, ids_file in (
            ("main", "umap_positions.json", "embeddings.npy", "embeddings_person_ids.json"),
            ("side", "side_umap_positions.json", "side_embeddings.npy", "side_embeddings_person_ids.json"),
        ):
            positions_path = ROOT / positions_file
            # The side space's files don't exist at all until enough people have opted in and a
            # full rebuild has run — treat that as "zero people embedded there", not an error.
            payload = json.loads(positions_path.read_text()) if positions_path.exists() else {"people": [], "generated_at": None}
            positions = {p["person_id"]: p for p in payload.get("people", [])}
            try:
                import numpy as np
                embeddings = np.load(ROOT / embeddings_file)
                embedding_ids = json.loads((ROOT / ids_file).read_text())
            except Exception:
                embeddings, embedding_ids = None, []
            spaces[key] = {
                "positions": positions, "generated_at": payload.get("generated_at"),
                "embeddings": embeddings, "embedding_ids": embedding_ids,
                "embedding_index": {person_id: i for i, person_id in enumerate(embedding_ids)},
                "match_cache": {},
            }

        with self.lock:
            self.people = people
            self.coauthors = coauthors
            self.generated_at = spaces["main"]["generated_at"]
            self._spaces = spaces

    def placement(self, person_id: str) -> dict:
        """Polled by index.html's "Creating/Updating your profile…" screen (waitForPlacement()) after
        a signup or bio edit: has this person's incremental placement landed? Answered from the
        catalogue — reloaded after every job, served no-store — rather than umap_positions.json,
        which CloudFront may hold for up to an hour. Raw UMAP x/y plus the map's extents so the
        client scales them exactly like every other dot; top matches co-author-filtered like matches()."""
        with self.lock:
            positions = self._spaces["main"]["positions"]
            p = positions.get(person_id)
            if not p:
                return {"placed": False}
            xs = [q["x"] for q in positions.values()]
            ys = [q["y"] for q in positions.values()]
            excluded = self.coauthors.get(person_id, set())
            return {"placed": True, "updatedAt": p.get("updated_at"), "x": p["x"], "y": p["y"],
                    "bounds": {"xMin": min(xs), "xMax": max(xs), "yMin": min(ys), "yMax": max(ys)},
                    "topMatches": [m for m in p.get("top_matches", []) if m["person_id"] not in excluded]}

    def matches(self, person_id: str, space: str = "main") -> list[dict]:
        with self.lock:
            excluded = self.coauthors.get(person_id, set()) if space == "main" else set()
            position = self._spaces[space]["positions"].get(person_id, {})
            result = []
            for match in position.get("top_matches", []):
                person = self.people.get(match["person_id"])
                if person and match["person_id"] not in excluded:
                    result.append({**person, "score": match["score"]})
            return result

    def match_page(self, person_id: str, offset: int, limit: int, student_only: bool = False, space: str = "main") -> tuple[list[dict], int]:
        """Compute once per attendee, then serve small pages from memory."""
        with self.lock:
            s = self._spaces[space]
            if person_id not in s["embedding_index"] or s["embeddings"] is None:
                matches = self.matches(person_id, space)
                if student_only:
                    matches = [m for m in matches if m.get("isStudent")]
                return matches, len(matches)
            embeddings, embedding_ids = s["embeddings"], s["embedding_ids"]
            i = s["embedding_index"][person_id]
            if person_id not in s["match_cache"]:
                import numpy as np
                excluded = self.coauthors.get(person_id, set()) if space == "main" else set()
                order = np.argsort(-(embeddings @ embeddings[i]))
                # Ranked row indices only (4 bytes each). Caching full per-match dicts cost ~0.5 MB per
                # attendee — 810 MB once everyone had looked — on a box that also holds the model.
                s["match_cache"][person_id] = np.array(
                    [j for j in order if j != i and embedding_ids[j] in self.people and embedding_ids[j] not in excluded],
                    dtype=np.int32)
            ranked = s["match_cache"][person_id]
            # ponytail: re-filtering on every call instead of caching per student_only variant —
            # cheap at ~1,650 people; cache separately if this scale grows a lot.
            if student_only:
                ranked = [j for j in ranked if self.people[embedding_ids[j]].get("isStudent")]
            page = [{**self.people[embedding_ids[j]], "score": round(float(embeddings[i] @ embeddings[j]) * 100)}
                    for j in ranked[offset:offset + limit]]
            return page, len(ranked)


STORE = Store()
CATALOGUE = Catalogue()
REBUILD_LOCK = threading.Lock()

# The warm embedding worker (`embed_umap.py worker`, systemd unit hfes-match-worker) listens here.
# It keeps the sentence model + JIT-compiled UMAP resident so a placement takes ~1 s instead of the
# ~30 s a cold subprocess needs (measured: ~20 s of that is imports + numba compile). Every job kind
# is offered to it first and falls back to the original cold subprocess if it's absent or dies
# mid-job — the worker is an accelerator, not a dependency.
WORKER_SOCKET = STATE / "worker.sock"


def _run_via_worker(kind: str, items: list | None = None, timeout: float = 600) -> bool:
    """Returns False when the worker isn't reachable or vanished mid-job (caller runs the cold
    subprocess instead). Raises RuntimeError when the worker ran the job and it failed."""
    import socket
    if not WORKER_SOCKET.exists():
        return False
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(5)
        sock.connect(str(WORKER_SOCKET))
    except OSError:
        sock.close()
        return False
    with sock:
        sock.settimeout(timeout)
        sock.sendall((json.dumps({"kind": kind, "items": items or []}) + "\n").encode())
        data = b""
        while not data.endswith(b"\n"):
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
    if not data:
        return False  # worker died mid-job; all its writes are atomic, so the cold path can safely redo it
    resp = json.loads(data)
    if not resp.get("ok"):
        raise RuntimeError(resp.get("error") or "worker reported failure without detail")
    return True
REBUILD_TIMER_LOCK = threading.Lock()
REBUILD_TIMER: threading.Timer | None = None


def row_json(row):
    if not row:
        return None
    return {"profileId": row["profile_id"], "personId": row["person_id"], "name": row["name"],
            "aff": row["affiliation"], "bio": row["bio"], "isStudent": bool(row["is_student"]),
            "sideInterest": row["side_interest"], "updatedAt": row["updated_at"]}


def start_rebuild(job_id: str | None = None, kind: str = "full") -> dict:
    if not REBUILD_LOCK.acquire(blocking=False):
        # ponytail: a rebuild is still running past this job's quiet window (measured
        # full rebuild ~2min vs. the 900s default, so this should be rare). Retry
        # shortly instead of leaving the job stuck "queued" forever; escalate to a
        # fixed max-retry/backoff only if real rebuild times ever approach the window.
        if job_id is not None:
            _arm_timer(job_id, 30)
        return {"started": False, "job": dict(STORE.current_job() or {})}
    if job_id is None:
        # No job_id means this is a fresh trigger with no pending-profiles list — either the
        # manual /api/admin/rebuild endpoint (kind="full") or the periodic side-only refit
        # (kind="side_only", see periodic_side_refit_check()). Either way the worker below does
        # the one full pass the caller asked for, not a per-profile incremental placement.
        job_id = secrets.token_urlsafe(12)
        with STORE.tx() as db:
            db.execute("INSERT INTO jobs(job_id,status,requested_at,detail) VALUES(%s,%s,%s,%s)",
                (job_id, "queued", utcnow(), "side_only" if kind == "side_only" else "[]"))

    def worker():
        try:
            with STORE.tx() as db:
                row = db.execute("SELECT detail FROM jobs WHERE job_id=%s", (job_id,)).fetchone()
                detail_raw = row["detail"] if row else None
                # "side_only" is a sentinel, not JSON — check for it before attempting to parse,
                # since json.loads("side_only") would raise and get silently swallowed into the
                # same except branch a legacy plain-string detail hits, running a full BOTH-spaces
                # rebuild by accident instead of the side-only one that was actually requested.
                side_only_refit = detail_raw == "side_only"
                if side_only_refit:
                    pending = []
                else:
                    try:
                        # A job queued by a pre-upgrade process has detail as a plain reason string
                        # ("profile changed"), not the JSON pending-list this version writes — treat
                        # that as "no pending list" (falls through to a full rebuild below) instead
                        # of crashing the worker on a deploy that lands mid-flight.
                        pending = json.loads(detail_raw) if detail_raw else []
                    except (json.JSONDecodeError, TypeError):
                        pending = []
                db.execute("UPDATE jobs SET status='running',started_at=%s WHERE job_id=%s", (utcnow(), job_id))
            # Rebuild work is intentionally isolated from request handling — every request
            # continues to use the last complete JSON while this runs.
            if pending:
                # Preserve the "conference structure": place every changed profile with
                # .transform() into the already-fitted UMAP space, in ONE subprocess (one
                # model load, one batch encode, one write) — this never refits UMAP and
                # never moves anyone else's dot. See embed_umap.py's incremental --batch.
                batch = [{"person_id": p.get("personId") or p["profileId"], "name": p["name"],
                          "text": p.get("bio") or p["name"], "side_text": p.get("sideInterest", "")} for p in pending]
                if not _run_via_worker("incremental", batch, timeout=10 * 60):
                    run = subprocess.run([sys.executable, str(ROOT / "embed_umap.py"), "incremental", "--batch"],
                        cwd=ROOT, input=json.dumps(batch), capture_output=True, text=True, timeout=10 * 60)
                    if run.returncode:
                        raise RuntimeError((run.stderr or run.stdout)[-3000:])
                detail = f"Placed {len(pending)} profile(s) without moving anyone else."
            elif side_only_refit:
                # Only the Hobby space's own UMAP/embeddings get refit — the academic map's
                # files are never touched by this command, so those positions can't move.
                if not _run_via_worker("side_only", timeout=4 * 60 * 60):
                    run = subprocess.run([sys.executable, str(ROOT / "embed_umap.py"), "full", "--side-only"], cwd=ROOT,
                        capture_output=True, text=True, timeout=4 * 60 * 60)
                    if run.returncode:
                        raise RuntimeError((run.stderr or run.stdout)[-3000:])
                detail = "Periodic Hobby-space refit complete (academic map untouched)."
            else:
                if not _run_via_worker("full", timeout=4 * 60 * 60):
                    run = subprocess.run([sys.executable, str(ROOT / "embed_umap.py"), "full"], cwd=ROOT,
                        capture_output=True, text=True, timeout=4 * 60 * 60)
                    if run.returncode:
                        raise RuntimeError((run.stderr or run.stdout)[-3000:])
                detail = "Full re-embed and UMAP refit complete."
            CATALOGUE.reload()
            status = "complete"
        except Exception as exc:
            status, detail = "failed", str(exc)[-3000:]
            send_alert_email(
                subject="[HFE Peapod] Rebuild job failed",
                body=(
                    f"A rebuild job failed and needs attention.\n\nJob ID: {job_id}\n\nError:\n{detail}\n\n"
                    "To fix: this usually doesn't need finding the specific broken profile — every "
                    "edit stays in SQLite regardless of this failure, and materialize_db_edits() "
                    "re-processes the ENTIRE profiles table on every run, not just what's new. So "
                    "triggering POST /api/admin/rebuild again typically resolves it, picking up "
                    "whatever got stuck along with everything else. Check GET /api/rebuild first if "
                    "you want to confirm the current state before retrying."
                ),
            )
        finally:
            with STORE.tx() as db:
                db.execute("UPDATE jobs SET status=%s,finished_at=%s,detail=%s WHERE job_id=%s", (status, utcnow(), detail, job_id))
            REBUILD_LOCK.release()
    threading.Thread(target=worker, name="rebuild", daemon=True).start()
    return {"started": True, "jobId": job_id}


def _arm_timer(job_id: str, delay: float) -> None:
    global REBUILD_TIMER
    with REBUILD_TIMER_LOCK:
        if REBUILD_TIMER:
            REBUILD_TIMER.cancel()
        REBUILD_TIMER = threading.Timer(max(0, delay), start_rebuild, args=(job_id,))
        REBUILD_TIMER.daemon = True
        REBUILD_TIMER.start()


def _merge_pending(existing_detail: str | None, profile: dict) -> str:
    """Fold one changed profile into a queued job's pending-placements list, deduped by
    person so editing twice inside the quiet window only places them once, with the latest text."""
    try:
        # Same pre-upgrade-format guard as start_rebuild's worker: a legacy plain-string
        # reason isn't JSON — treat it as an empty pending list rather than crashing.
        pending = json.loads(existing_detail) if existing_detail else []
    except (json.JSONDecodeError, TypeError):
        pending = []
    key = profile.get("personId") or profile["profileId"]
    pending = [p for p in pending if (p.get("personId") or p["profileId"]) != key]
    pending.append({"profileId": profile["profileId"], "personId": profile.get("personId"),
                     "name": profile["name"], "bio": profile.get("bio", ""),
                     "sideInterest": profile.get("sideInterest", "")})
    return json.dumps(pending)


def queue_rebuild(profile: dict) -> dict:
    """Queue this profile for incremental placement without letting web traffic start model work.

    Several edits inside the quiet window coalesce into one job's pending list (deduped by
    person) rather than causing several expensive subprocess calls.  Each pending profile is
    later placed with its own `embed_umap.py incremental` call — the position is frozen once
    placed, so this never refits UMAP or moves anyone else's dot.  The current static embedding
    file remains live meanwhile.
    """
    current = STORE.current_job()
    if current and current["status"] in ("queued", "running"):
        if current["status"] == "queued":
            with STORE.tx() as db:
                row = db.execute("SELECT detail FROM jobs WHERE job_id=%s", (current["job_id"],)).fetchone()
                db.execute("UPDATE jobs SET detail=%s WHERE job_id=%s",
                    (_merge_pending(row["detail"] if row else None, profile), current["job_id"]))
            if REBUILD_DELAY_SECONDS:
                _arm_timer(current["job_id"], REBUILD_DELAY_SECONDS)
            return {"queued": True, "jobId": current["job_id"], "coalesced": True}
        # A rebuild is already running for an earlier edit. This edit needs its
        # own follow-up job so it isn't silently dropped once the running one
        # finishes; further edits before that follow-up fires coalesce into it
        # via the "queued" branch above.
        job_id = secrets.token_urlsafe(12)
        with STORE.tx() as db:
            db.execute("INSERT INTO jobs(job_id,status,requested_at,detail) VALUES(%s,%s,%s,%s)",
                (job_id, "queued", utcnow(), _merge_pending(None, profile)))
        if REBUILD_DELAY_SECONDS:
            _arm_timer(job_id, REBUILD_DELAY_SECONDS)
        return {"queued": True, "jobId": job_id, "coalesced": False}
    job_id = secrets.token_urlsafe(12)
    with STORE.tx() as db:
        db.execute("INSERT INTO jobs(job_id,status,requested_at,detail) VALUES(%s,%s,%s,%s)",
            (job_id, "queued", utcnow(), _merge_pending(None, profile)))
    if REBUILD_DELAY_SECONDS:
        _arm_timer(job_id, REBUILD_DELAY_SECONDS)
    return {"queued": True, "jobId": job_id, "coalesced": False}


app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_BODY
# Pinned in code: Flask otherwise reads FLASK_DEBUG from the environment. Debug mode never runs here.
app.debug = False


def json_response(status, body):
    resp = jsonify(body)
    resp.status_code = int(status)
    resp.headers["Cache-Control"] = "no-store"
    return resp


def read_json():
    return json.loads(request.get_data() or b"{}")


def visitor_ip() -> str:
    # Nginx proxies to this process over loopback, so remote_addr is always 127.0.0.1 in
    # production; the real visitor is in the forwarded headers (see client_ip).
    return client_ip(request.headers, request.remote_addr or "")


@app.errorhandler(404)
@app.errorhandler(405)
def _not_found(_exc):
    return json_response(HTTPStatus.NOT_FOUND, {"error": "Not found"})


@app.errorhandler(413)
def _too_large(_exc):
    return json_response(HTTPStatus.BAD_REQUEST, {"error": "Request body is too large."})


@app.errorhandler(Exception)
def _unhandled(exc):
    print(f"unhandled error on {request.method} {request.path}: {exc!r}", file=sys.stderr, flush=True)
    return json_response(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "Internal server error"})


@app.get("/api/health")
def api_health():
    return json_response(HTTPStatus.OK, {"ok": True, "generatedAt": CATALOGUE.generated_at})


@app.get("/api/matches")
def api_matches():
    person_id = request.args.get("personId", "")
    try:
        offset = max(0, int(request.args.get("offset", "0")))
        limit = min(100, max(1, int(request.args.get("limit", "50"))))
    except ValueError:
        return json_response(HTTPStatus.BAD_REQUEST, {"error": "offset and limit must be integers."})
    student_only = request.args.get("studentOnly", "0") == "1"
    space = request.args.get("space", "main")
    if space not in ("main", "side"):
        space = "main"
    matches, total = CATALOGUE.match_page(person_id, offset, limit, student_only, space)
    return json_response(HTTPStatus.OK, {"matches": matches, "offset": offset, "total": total, "generatedAt": CATALOGUE.generated_at})


@app.get("/api/placement")
def api_placement():
    return json_response(HTTPStatus.OK, CATALOGUE.placement(request.args.get("personId", "")))


@app.get("/api/rebuild")
def api_rebuild_status():
    job = STORE.current_job()
    return json_response(HTTPStatus.OK, {"job": dict(job) if job else None})


@app.post("/api/profile")
def api_profile():
    if not profile_write_allowed(visitor_ip(), time.monotonic()):
        return json_response(HTTPStatus.TOO_MANY_REQUESTS, {"error": "Too many profile updates from this connection. Please try again in a few minutes."})
    try:
        body = read_json()
        if not isinstance(body, dict):
            raise ValueError("Profile body must be a JSON object.")
        profile = STORE.upsert_profile(body)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        return json_response(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
    # This endpoint receives only an explicit profile revision.  Browser
    # identity and saved contacts are deliberately localStorage-only.
    return json_response(HTTPStatus.OK, {"profile": row_json(profile), "embeddingRefresh": queue_rebuild(row_json(profile))})


@app.post("/api/feedback")
def api_feedback():
    try:
        STORE.add_feedback(str(read_json().get("message", "")))
        return json_response(HTTPStatus.CREATED, {"ok": True})
    except (ValueError, AttributeError, json.JSONDecodeError) as exc:
        return json_response(HTTPStatus.BAD_REQUEST, {"error": str(exc)})


@app.post("/api/admin/rebuild")
def api_admin_rebuild():
    if not ADMIN_TOKEN or not secrets.compare_digest(request.headers.get("X-Rebuild-Token", ""), ADMIN_TOKEN):
        return json_response(HTTPStatus.FORBIDDEN, {"error": "Invalid rebuild token."})
    return json_response(HTTPStatus.ACCEPTED, start_rebuild())


@app.post("/api/draft-bio")
def api_draft_bio():
    try:
        body = read_json()
    except json.JSONDecodeError as exc:
        return json_response(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
    name = str(body.get("name", "")).strip()[:200]
    aff = str(body.get("aff", "")).strip()[:500]
    if not name:
        return json_response(HTTPStatus.BAD_REQUEST, {"error": "Name is required."})
    ip = visitor_ip()
    with _DRAFT_BIO_LOCK:
        last = _draft_bio_last_call.get(ip, 0.0)
        now = time.monotonic()
        if now - last < DRAFT_BIO_COOLDOWN_SECONDS:
            return json_response(HTTPStatus.TOO_MANY_REQUESTS, {"bio": None})
        _draft_bio_last_call[ip] = now
    return json_response(HTTPStatus.OK, {"bio": draft_bio(name, aff)})


@app.post("/api/client-error")
def api_client_error():
    try:
        body = read_json()
    except json.JSONDecodeError as exc:
        return json_response(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
    message = str(body.get("message", "")).strip()[:2000]
    global _last_client_error_alert
    with _CLIENT_ERROR_LOCK:
        now = time.monotonic()
        if now - _last_client_error_alert >= CLIENT_ERROR_COOLDOWN_SECONDS:
            _last_client_error_alert = now
            send_alert_email(
                subject="[HFE Peapod] Attendees are seeing a load error",
                body=(
                    "A browser reported it could not load the app's data (CSVs/embeddings) "
                    f"at all — attendees are seeing the 'Error! :(' screen.\n\nDetail:\n{message}\n\n"
                    "Check GET /api/health and that Nginx/CloudFront are serving authors.csv "
                    "and umap_positions.json normally."
                ),
            )
    return json_response(HTTPStatus.OK, {"ok": True})

def side_refit_due() -> bool:
    """Pure/testable: true if the side-interest space's last full refit is old enough (or has
    never happened at all) to warrant another one. Reads only side_umap_positions.json's own
    generated_at — no side effects, no dependence on the rebuild lock or job queue, so this can
    be asserted against directly without spinning up a subprocess or a model load."""
    path = ROOT / "side_umap_positions.json"
    if not path.exists():
        # Never fitted yet — includes "nobody's opted in at all," in which case the periodic
        # attempt below is cheap: embed_umap.py skips before ever loading the model if there
        # aren't enough people, so this isn't wasted work, just a quick no-op most cycles.
        return True
    try:
        generated_at = json.loads(path.read_text()).get("generated_at")
    except (json.JSONDecodeError, OSError):
        return True
    if not generated_at:
        return True
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(generated_at)).total_seconds()
    return age >= SIDE_REFIT_INTERVAL_SECONDS


def _daemon_timer(delay: float, fn) -> None:
    # Non-daemon timers keep a gunicorn worker alive after SIGTERM until graceful_timeout kills it,
    # turning every restart into ~30 s of downtime.
    t = threading.Timer(delay, fn)
    t.daemon = True
    t.start()


def periodic_side_refit_check() -> None:
    """Runs every SIDE_REFIT_CHECK_SECONDS, forever, on a daemon timer. Silent by design — same
    as any other rebuild, nothing announces this to users; it only ever shows up in GET
    /api/rebuild's job history and (only on failure) the alert email.

    Mindful of the existing queue rather than competing with it: if a real edit's incremental
    placement is already queued or running, this cycle skips entirely and simply checks again
    next time — it never preempts, cancels, or races a user-driven rebuild. Only the Hobby space
    is touched (see embed_umap.py's --side-only) — the academic map never refits on this cadence.
    """
    try:
        current = STORE.current_job()
        if current and current["status"] in ("queued", "running"):
            return
        if side_refit_due():
            start_rebuild(kind="side_only")
    finally:
        _daemon_timer(SIDE_REFIT_CHECK_SECONDS, periodic_side_refit_check)


def new_feed_people() -> list[dict] | None:
    """Pending-list entries for people in authors.csv who have no embedding yet, or None when
    nothing has been embedded at all (first sync — that needs a full fit, not 9,000 placements)."""
    ids_path = ROOT / "embeddings_person_ids.json"
    if not ids_path.exists():
        return None
    embedded = set(json.loads(ids_path.read_text()))
    with open(ROOT / "authors.csv", encoding="utf-8-sig", newline="") as f:
        return [{"personId": r["Person ID"], "profileId": r["Person ID"], "name": r["Author"],
                 "bio": r["s_profile"] or r["Interest Profile"] or r["Author"], "sideInterest": r.get("Side Interest", "")}
                # Chairs with no papers have no text; a dot placed from a name alone means nothing.
                for r in csv.DictReader(f) if r.get("Person ID") and r["Person ID"] not in embedded
                and (r["s_profile"] or r["Interest Profile"])]


def periodic_feed_check() -> None:
    """Every FEED_CHECK_SECONDS: if the CHI feed has a new revision, rewrite authors.csv /
    program.csv from it and place only the people who are new. Existing dots never move —
    same frozen-map rule as signups; changed abstracts wait for the next deliberate full run.
    Holds REBUILD_LOCK while writing, because jobs write authors.csv too; a busy lock just
    means try again next cycle."""
    try:
        if not REBUILD_LOCK.acquire(blocking=False):
            return
        try:
            import chi_feed
            result = chi_feed.sync()
            if result["changed"]:
                CATALOGUE.reload()
        finally:
            REBUILD_LOCK.release()
        if not result["changed"]:
            return
        pending = new_feed_people()
        if pending is None:
            start_rebuild(kind="full")
        elif pending:
            job_id = secrets.token_urlsafe(12)
            with STORE.tx() as db:
                db.execute("INSERT INTO jobs(job_id,status,requested_at,detail) VALUES(%s,%s,%s,%s)",
                    (job_id, "queued", utcnow(), json.dumps(pending)))
            start_rebuild(job_id)
    except Exception as exc:
        # A feed outage must never take the app down — the last good files keep serving.
        print(f"periodic_feed_check: {exc.__class__.__name__}: {exc}", flush=True)
    finally:
        _daemon_timer(FEED_CHECK_SECONDS, periodic_feed_check)


def recover_jobs() -> None:
    """Re-arm the quiet-window timer after a service restart.

    The timer counting down a queued rebuild lives only in process memory; a
    restart (deploy, crash, reboot) loses it while the job row survives in
    SQLite as still "queued" or "running", with nothing left to ever start it.
    """
    job = STORE.current_job()
    if not job:
        return
    if job["status"] == "running":
        # The process that was running this rebuild is gone; nothing will ever
        # flip it to complete/failed, and REBUILD_LOCK is a fresh, unheld lock
        # in this new process, so a stuck "running" row would otherwise block
        # queue_rebuild's running-vs-queued branching forever.
        with STORE.tx() as db:
            db.execute("UPDATE jobs SET status='failed',finished_at=%s,detail=%s WHERE job_id=%s",
                (utcnow(), "Interrupted by service restart.", job["job_id"]))
        return
    if job["status"] == "queued" and REBUILD_DELAY_SECONDS:
        requested = datetime.fromisoformat(job["requested_at"])
        elapsed = (datetime.now(timezone.utc) - requested).total_seconds()
        _arm_timer(job["job_id"], REBUILD_DELAY_SECONDS - elapsed)


def start_background() -> None:
    """Once per server process: re-arm any queued job lost to the restart, then start the
    periodic Hobby-space refit check (short initial delay so it doesn't compete with startup)."""
    recover_jobs()
    _daemon_timer(SIDE_REFIT_INITIAL_DELAY_SECONDS, periodic_side_refit_check)
    if FEED_CHECK_SECONDS:
        _daemon_timer(5, periodic_feed_check)


def main():
    """Local development only — production runs gunicorn (see deploy/gunicorn.conf.py)."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    start_background()
    print(f"HFES API listening on {args.host}:{args.port}", flush=True)
    app.run(host=args.host, port=args.port, threaded=True, debug=False)

if __name__ == "__main__":
    main()

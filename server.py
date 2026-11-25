#!/usr/bin/env python3
"""CHI Peapod API server. Nginx serves the static files; this process answers the JSON API and
keeps the catalogue in memory. Runs as ONE process with many threads: the rebuild timer, REBUILD_LOCK
and the rate-limit windows live in process memory."""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import secrets
import shlex
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from pathlib import Path

import psycopg
from flask import Flask, jsonify, request
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parent
STATE = Path(os.environ.get("HFES_STATE_DIR", ROOT / "state"))
# libpq connection string; the default is the local Unix socket with peer auth (no password).
DATABASE_URL = os.environ.get("HFES_DATABASE_URL", "dbname=hfes")
ADMIN_TOKEN = os.environ.get("HFES_REEMBED_TOKEN", "")
# Read-only dashboard password (owner-chosen, short, typed on phones). Separate from the rebuild token on
# purpose: leaking it exposes a traffic graph, not a full re-embed. The rebuild token is accepted too.
DASHBOARD_TOKEN = os.environ.get("HFES_DASHBOARD_TOKEN", "")
MAX_BODY = 64 * 1024
REBUILD_DELAY_SECONDS = max(0, int(os.environ.get("HFES_AUTO_REBUILD_SECONDS", "900")))

# The Hobby space starts empty and grows as people opt in, so it gets refit periodically.
# The academic map never does: it's frozen by design.
SIDE_REFIT_INTERVAL_SECONDS = max(0, int(os.environ.get("HFES_SIDE_REFIT_INTERVAL_SECONDS", str(24 * 60 * 60))))
# 0 = off: the Hobby view is a browser-side word cloud, so the side UMAP is never displayed.
SIDE_REFIT_CHECK_SECONDS = int(os.environ.get("HFES_SIDE_REFIT_CHECK_SECONDS", "0"))
if SIDE_REFIT_CHECK_SECONDS: SIDE_REFIT_CHECK_SECONDS = max(60, SIDE_REFIT_CHECK_SECONDS)
# Programme feed poll interval (chi_feed.py); 0 disables it.
FEED_CHECK_SECONDS = max(0, int(os.environ.get("HFES_FEED_CHECK_SECONDS", "60")))
SIDE_REFIT_INITIAL_DELAY_SECONDS = max(1, int(os.environ.get("HFES_SIDE_REFIT_INITIAL_DELAY_SECONDS", "60")))

# Optional alert email on a failed rebuild. Unset = does nothing.
ALERT_EMAIL_TO = os.environ.get("HFES_ALERT_EMAIL_TO", "")
ALERT_SMTP_HOST = os.environ.get("HFES_ALERT_SMTP_HOST", "smtp.gmail.com")
ALERT_SMTP_PORT = int(os.environ.get("HFES_ALERT_SMTP_PORT", "587"))
ALERT_SMTP_USER = os.environ.get("HFES_ALERT_SMTP_USER", "")
ALERT_SMTP_PASSWORD = os.environ.get("HFES_ALERT_SMTP_PASSWORD", "")


def send_alert_email(subject: str, body: str) -> None:
    """Best-effort: never raises, so a failed alert can't mask the original error."""
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

# Optional: draft a bio for a new signup with a web-search-backed Haiku call. Unset = disabled.
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
DRAFT_BIO_SYSTEM_PROMPT = """You are drafting a STARTING POINT for a conference attendee's research-interest \
paragraph — not a finished biography. They will see their own name directly above this text and are \
expected to edit it; the goal is a spark to react to. You will be given a name and an affiliation.

Use the web_search tool to find reliable, directly relevant information: prefer the person's own \
personal or faculty page, their university's or company's official directory/profile page, or their \
own publications/lab page. Do not use social-media aggregators, unrelated news mentions, or pages \
about a different person who happens to share the name. Do a small number of targeted searches, not a \
broad scour.

This is for an attendee of ACM CHI (the Conference on Human Factors in Computing Systems). If your search \
turns up more than one real person matching this name and affiliation, prefer whichever one works in \
human-computer interaction or a closely related field (interaction design, accessibility, social \
computing, CSCW, UX research, human-AI interaction) — that person is far more likely to be the actual \
attendee. Only use this to choose between genuine candidates you actually found; never invent an HCI connection.

If you cannot confidently confirm you found the right person (name and affiliation both line up, or \
there is strong contextual evidence), your entire final response must be exactly the single word \
NO_MATCH — no explanation, no reasoning, nothing else.

Otherwise write ONE coherent, name-free research-interest paragraph. It is shown to readers as-is and \
embedded for shared-interest matching, so it must read like the other profiles in the app:

Content: Open with the person's central research question or interest, stated as a concrete topic. \
Preserve specific questions, methods, measures, populations and settings that distinguish the work. \
If the sources give substantial attention to a second research line, keep it in at least one sentence. \
Use established technical terms accurately. Preserve the sources' degree of certainty; a suggested or \
possible interest must not become an established program. Do not invent mechanisms, outcomes, \
examples, methods or applications you could not verify.

Style: One paragraph of 90–120 words when the sources support that length; when they cannot support \
90 accurate words, return a shorter complete paragraph rather than padding. Clear scientific prose, \
concrete subjects. No title or headline, no Markdown, no names, no author-referential pronouns \
("she", "his work"), no promotional claims, no rhetorical questions, no generic statements about why \
the topic matters. Avoid repeated frames such as "This research", "The work", "Research focuses on". \
Avoid contrast slogans and filler such as "not merely X, but Y" or "Rather than X". Do not mention \
the conference, the search, the sources, missing information, or that anything was inferred.

Your entire final response must be only that paragraph — no preamble like "I'll search for..." or \
"Based on the results...", no extra paragraphs, no word count, no meta-commentary."""

# No per-IP limit on drafts: venue Wi-Fi puts many attendees behind one IP. The spend guard is the
# limit in the Anthropic Console.


def client_ip(headers, fallback: str) -> str:
    """Behind CloudFront, X-Real-IP is the edge; the viewer is the last X-Forwarded-For hop, which
    the viewer can't forge."""
    xff = headers.get("X-Forwarded-For", "")
    return xff.rsplit(",", 1)[-1].strip() or headers.get("X-Real-IP") or fallback


# POST /api/profile is unauthenticated (good-faith claiming); this cap is what stops a script from
# overwriting every bio. In-memory sliding window, resets on restart.
_PROFILE_WRITE_LOCK = threading.Lock()
_profile_writes: dict[str, list[float]] = {}
# Sized for a venue where the whole hall shares one public IP (owner's note, 2026-10-07): an opening-session
# rush of a few hundred signups in ten minutes must not trip it, while a script hammering the endpoint still
# does. Each write is one cheap upsert plus a coalesced placement job, so 500 is not a load concern.
PROFILE_WRITES_PER_WINDOW = 500
PROFILE_WINDOW_SECONDS = 600


def profile_write_allowed(ip: str, now: float) -> bool:
    with _PROFILE_WRITE_LOCK:
        recent = [t for t in _profile_writes.get(ip, []) if now - t < PROFILE_WINDOW_SECONDS]
        if len(recent) >= PROFILE_WRITES_PER_WINDOW:
            _profile_writes[ip] = recent
            return False
        _profile_writes[ip] = recent + [now]
        return True

# /api/client-error alerts on a real outage. Global cooldown, not per-IP: every visitor hits it at once.
_CLIENT_ERROR_LOCK = threading.Lock()
_last_client_error_alert = 0.0
CLIENT_ERROR_COOLDOWN_SECONDS = 120  # owner wants to hear early and often when something needs them


def draft_bio(name: str, aff: str) -> str | None:
    """Ask Haiku (with web search) for a ~100-word bio. None if disabled, no match, or any failure."""
    if not ANTHROPIC_API_KEY:
        return None
    body = json.dumps({
        "model": "claude-haiku-4-5",
        "max_tokens": 3000,
        # Thinking keeps the search narration out of the visible text blocks.
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
    # The answer is every text block after the last tool/thinking block; it can span more than one.
    content = data.get("content", [])
    last_non_text_idx = max((i for i, b in enumerate(content) if b.get("type") != "text"), default=-1)
    text = "".join(b.get("text", "") for i, b in enumerate(content)
                   if i > last_non_text_idx and b.get("type") == "text").strip()
    text = re.sub(r"^[.\s]+", "", text)
    # A preamble sentence can precede the answer; keep the last paragraph and drop stray Markdown.
    text = re.split(r"\n\s*\n", text.strip())[-1].replace("**", "").strip()
    # NO_MATCH anywhere counts as a decline; the model sometimes narrates around the sentinel.
    if not text or re.search(r"\bNO_MATCH\b", text):
        return None
    return text[:2000]  # defensive cap; the prompt already asks for 90-120 words


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
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS pending TEXT;  -- the job's original detail, kept across retries
CREATE TABLE IF NOT EXISTS feedback (
  feedback_id TEXT PRIMARY KEY, message TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS metrics (
  bucket TEXT NOT NULL, kind TEXT NOT NULL, n INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (bucket, kind)
);
DO $$ BEGIN  -- 2026-10-07: buckets went from hour (13 chars) to minute (16 chars); old rows stay summable
  IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='metrics' AND column_name='hour')
  THEN ALTER TABLE metrics RENAME COLUMN hour TO bucket; END IF; END $$;
CREATE TABLE IF NOT EXISTS profile_edits (
  edit_id TEXT PRIMARY KEY, profile_id TEXT NOT NULL, person_id TEXT,
  change_json TEXT NOT NULL, created_at TEXT NOT NULL
);
"""
# Timestamps are ISO-8601 TEXT: the browser compares updatedAt strings to detect a placement.


class Store:
    # One shared connection serialised by self.lock; every query is sub-millisecond.
    def __init__(self) -> None:
        STATE.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self._conn = None
        with self.tx() as db:
            db.execute(SCHEMA)

    @property
    def conn(self):
        # Reconnect after a Postgres restart.
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
        # Two embedding spaces of the same shape: "main" (academic) and "side" (optional hobbies).
        self._spaces: dict[str, dict] = {"main": {}, "side": {}}
        self.reload()

    def reload(self) -> None:
        with open(ROOT / "authors.csv", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        # Same bio precedence as embed_umap.py's bio_text().
        people = {r["Person ID"]: {"personId": r["Person ID"], "name": r["Author"], "aff": r["Affiliation"],
            "bio": r.get("s_profile") or r.get("Interest Profile") or r.get("Presentation Titles") or "No profile on file.",
            "isStudent": (r.get("Is Student") or "").strip() == "1",
            "sideInterest": r.get("Side Interest") or "",
            "claimed": (r.get("Claimed") or "").strip() == "1"}
            for r in rows if r.get("Author") and r.get("Person ID")}

        from embed_umap import coauthor_index
        coauthors = coauthor_index(rows)

        spaces = {}
        for key, positions_file, embeddings_file, ids_file in (
            ("main", "umap_positions.json", "embeddings.npy", "embeddings_person_ids.json"),
            ("side", "side_umap_positions.json", "side_embeddings.npy", "side_embeddings_person_ids.json"),
        ):
            positions_path = ROOT / positions_file
            # The side space's files don't exist until enough people have opted in.
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
        """Has this person's placement landed? Served no-store from memory, since CloudFront may hold
        umap_positions.json for an hour. Returns raw x/y plus the map extents so the client scales it."""
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
                # Cache row indices only; full dicts cost ~0.5 MB per attendee.
                s["match_cache"][person_id] = np.array(
                    [j for j in order if j != i and embedding_ids[j] in self.people and embedding_ids[j] not in excluded],
                    dtype=np.int32)
            ranked = s["match_cache"][person_id]
            if student_only:
                ranked = [j for j in ranked if self.people[embedding_ids[j]].get("isStudent")]
            page = [{**self.people[embedding_ids[j]], "score": round(float(embeddings[i] @ embeddings[j]) * 100)}
                    for j in ranked[offset:offset + limit]]
            return page, len(ranked)


STORE = Store()
CATALOGUE = Catalogue()
REBUILD_LOCK = threading.Lock()

# The warm worker (`embed_umap.py worker`) keeps the model + UMAP loaded so a placement takes ~1 s
# instead of ~30 s. Every job tries it first and falls back to a cold subprocess.
WORKER_SOCKET = STATE / "worker.sock"


def _run_via_worker(kind: str, items: list | None = None, timeout: float = 600) -> bool:
    """False if the worker is absent or died mid-job (caller falls back); RuntimeError if it ran and failed."""
    import socket
    # Any OS-level trouble reaching the socket (missing, refused, or — seen 2026-10-07 while a deploy's
    # bootstrap was re-chowning the tree — EACCES on the stat itself) means "use the cold path", never a
    # failed job: the worker is an accelerator, not a dependency.
    try:
        if not WORKER_SOCKET.exists():
            return False
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(5)
            sock.connect(str(WORKER_SOCKET))
        except OSError:
            sock.close()
            return False
    except OSError:
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
# A failed job is retried automatically (owner's call, 2026-10-07): most failures seen so far were timing
# (a deploy restart, the worker not warm yet) and pass on the next try. After MAX_ATTEMPTS the job is
# final: `failed`, the owner gets an email with ready-to-run fix commands, and the person's browser is
# told (via /api/placement) to ask them to email in.
MAX_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 30
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
        # A rebuild is still running: retry shortly rather than leave the job queued forever.
        if job_id is not None:
            _arm_timer(job_id, 30)
        return {"started": False, "job": dict(STORE.current_job() or {})}
    if job_id is None:
        # No job_id: a manual full rebuild or the periodic side-only refit, not a placement list.
        job_id = secrets.token_urlsafe(12)
        with STORE.tx() as db:
            db.execute("INSERT INTO jobs(job_id,status,requested_at,detail) VALUES(%s,%s,%s,%s)",
                (job_id, "queued", utcnow(), "side_only" if kind == "side_only" else "[]"))

    def worker():
        try:
            with STORE.tx() as db:
                row = db.execute("SELECT detail FROM jobs WHERE job_id=%s", (job_id,)).fetchone()
                detail_raw = row["detail"] if row else None
                # "side_only" is a sentinel, not JSON; check before parsing or it falls into the full path.
                side_only_refit = detail_raw == "side_only"
                if side_only_refit:
                    pending = []
                else:
                    try:
                        # Older jobs stored a plain reason string here; treat that as no pending list.
                        pending = json.loads(detail_raw) if detail_raw else []
                    except (json.JSONDecodeError, TypeError):
                        pending = []
                db.execute("UPDATE jobs SET status='running',started_at=%s,attempts=attempts+1,pending=COALESCE(pending,%s) WHERE job_id=%s",
                           (utcnow(), detail_raw, job_id))
            if pending:
                # Incremental: transform into the fitted UMAP space in one batch; nobody else's dot moves.
                batch = [{"person_id": p.get("personId") or p["profileId"], "name": p["name"],
                          "text": p.get("bio") or p["name"], "side_text": p.get("sideInterest", "")} for p in pending]
                if not _run_via_worker("incremental", batch, timeout=10 * 60):
                    run = subprocess.run([sys.executable, str(ROOT / "embed_umap.py"), "incremental", "--batch"],
                        cwd=ROOT, input=json.dumps(batch), capture_output=True, text=True, timeout=10 * 60)
                    if run.returncode:
                        raise RuntimeError((run.stderr or run.stdout)[-3000:])
                detail = f"Placed {len(pending)} profile(s) without moving anyone else."
            elif side_only_refit:
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
            error = str(exc)[-3000:]
            with STORE.tx() as db:
                row = db.execute("SELECT attempts, pending FROM jobs WHERE job_id=%s", (job_id,)).fetchone()
            attempts = (row["attempts"] if row else MAX_ATTEMPTS)
            if attempts < MAX_ATTEMPTS:
                # Back to the queue with its original pending list, so a signup arriving meanwhile
                # coalesces into it as usual; the timer re-runs it shortly.
                status, detail = "queued", (row["pending"] if row else None)
                print(f"job {job_id} failed (attempt {attempts}/{MAX_ATTEMPTS}), retrying in {RETRY_DELAY_SECONDS}s: {error}", file=sys.stderr)
                if attempts == 1:  # heads-up on the first failure (owner's call: conservative); no action needed yet
                    send_alert_email(subject="[CHI Peapod] FYI: a placement job failed once, retrying",
                                     body=f"Job {job_id} failed on attempt 1 of {MAX_ATTEMPTS} and will retry in {RETRY_DELAY_SECONDS}s.\n"
                                          f"No action needed unless a second email says it was given up on.\n\nError:\n{error}\n")
                _arm_timer(job_id, RETRY_DELAY_SECONDS)
            else:
                status, detail = "failed", error
                send_alert_email(subject=f"[CHI Peapod] Placement failed after {attempts} attempts",
                                 body=failure_email(job_id, row["pending"] if row else None, error))
        finally:
            with STORE.tx() as db:
                if status == "queued":
                    db.execute("UPDATE jobs SET status='queued',detail=%s WHERE job_id=%s", (detail, job_id))
                else:
                    db.execute("UPDATE jobs SET status=%s,finished_at=%s,detail=%s WHERE job_id=%s", (status, utcnow(), detail, job_id))
            REBUILD_LOCK.release()
    threading.Thread(target=worker, name="rebuild", daemon=True).start()
    return {"started": True, "jobId": job_id}


def failure_email(job_id: str, pending_raw: str | None, error: str) -> str:
    """What the owner reads at 7 am: what broke, who is affected, and commands to paste."""
    try:
        pending = json.loads(pending_raw) if pending_raw and pending_raw != "side_only" else []
    except (json.JSONDecodeError, TypeError):
        pending = []
    lines = [f"A placement job failed {MAX_ATTEMPTS} times in a row and has been given up on.",
             f"Job ID: {job_id}", "", "Error (last attempt):", error, ""]
    if pending_raw == "side_only":
        lines += ["This was the hourly Hobby-map refit. Nobody is waiting on it; it runs again next hour.",
                  "Only act if it keeps failing every hour: sudo journalctl -u hfes-match-worker --since '3 hours ago'"]
    elif not pending:
        lines += ["This was a FULL rebuild. The old map is still being served. Re-run it in a quiet moment:",
                  "  curl -XPOST -H \"X-Rebuild-Token: $HFES_REEMBED_TOKEN\" http://localhost/api/profile  # (on the server, after: set -a; . /etc/hfes-match.env)".replace("/api/profile", "/api/admin/rebuild")]
    else:
        lines += [f"{len(pending)} profile(s) are NOT on the map yet (their browsers have been told to email you):",
                  *[f"  - {p.get('name', '?')} ({p.get('aff', '')}) id {p.get('personId') or p.get('profileId')}" for p in pending], "",
                  "WHAT TO DO", "1. Check the site is up: https://d1j7v5u0jmb8lf.cloudfront.net/api/health",
                  "2. On the server (ssh hfe-peapod), paste these to queue each person again through the normal path:"]
        for p in pending:
            body = json.dumps({k: p.get(k, "") for k in ("profileId", "personId", "name", "aff", "bio", "sideInterest")} | {"isStudent": bool(p.get("isStudent"))})
            lines.append("  curl -s -XPOST -H 'Content-Type: application/json' http://localhost/api/profile -d " + shlex.quote(body))
        lines += ["3. Wait ~10 s, then check http://localhost/api/rebuild shows status complete.",
                  "4. If it fails AGAIN with the same error, it is a bug, not a hiccup: stop retrying and send this email",
                  "   plus the output of: sudo journalctl -u hfes-match -u hfes-match-worker --since '1 hour ago'"]
    return "\n".join(lines)


def _arm_timer(job_id: str, delay: float) -> None:
    global REBUILD_TIMER
    with REBUILD_TIMER_LOCK:
        if REBUILD_TIMER:
            REBUILD_TIMER.cancel()
        REBUILD_TIMER = threading.Timer(max(0, delay), start_rebuild, args=(job_id,))
        REBUILD_TIMER.daemon = True
        REBUILD_TIMER.start()


def _merge_pending(existing_detail: str | None, profile: dict) -> str:
    """Add a changed profile to a queued job's pending list, deduped by person (latest text wins)."""
    try:
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
    """Queue this profile for incremental placement. Edits inside the quiet window coalesce into one job."""
    # Read-then-decide must be atomic: two signups in the same millisecond would each insert a job
    # and only the last-armed timer would fire. RLock, so tx() nests.
    with STORE.lock:
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
            # A rebuild is running: this edit needs its own follow-up job or it's dropped.
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


# Anonymous usage counters for /dashboard-traffic: one row per UTC minute per kind, incremented by a
# fire-and-forget beacon from index.html (track()) or by the server itself. Deliberately stores no IP,
# user agent, person id, search text or exact timestamp — nothing in this table can be traced back.
EVENT_KINDS = frozenset("""
open_first open_returning lookup_found lookup_suggested lookup_none claimed created edit
draft_bio_ok draft_bio_none search open_person open_program save_contact save_program show_more
view_everyone view_student view_side faq placement_failed placement_timeout
askhobby_added askhobby_skip askbio_ok askbio_edit open_hobby
""".split())


def bump(kind: str) -> None:
    if kind not in EVENT_KINDS:
        return
    try:
        with STORE.tx() as db:
            db.execute("INSERT INTO metrics(bucket,kind,n) VALUES(%s,%s,1) "
                       "ON CONFLICT (bucket,kind) DO UPDATE SET n = metrics.n + 1", (utcnow()[:16], kind))
    except Exception as exc:  # a counter must never break a real request
        print(f"bump: {exc}", file=sys.stderr)


def admin_ok(dashboard: bool = False) -> bool:
    given = request.headers.get("X-Rebuild-Token", "") or request.args.get("token", "")
    ok = bool(ADMIN_TOKEN) and secrets.compare_digest(given, ADMIN_TOKEN)
    if dashboard and DASHBOARD_TOKEN:
        ok = ok or secrets.compare_digest(given, DASHBOARD_TOKEN)
    return ok


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
    person_id = request.args.get("personId", "")
    result = CATALOGUE.placement(person_id)
    if not result["placed"] and person_id:
        # Not on the map yet: is that because their job is still being retried, or finally failed?
        needle = f"%{json.dumps(person_id)}%"  # the id as it appears quoted inside the pending JSON
        with STORE.tx() as db:
            job = db.execute("SELECT status, attempts FROM jobs WHERE pending LIKE %s OR detail LIKE %s "
                             "ORDER BY requested_at DESC LIMIT 1", (needle, needle)).fetchone()
        if job:
            result["jobStatus"] = job["status"]
            result["failed"] = job["status"] == "failed"
    return json_response(HTTPStatus.OK, result)


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
    bio = draft_bio(name, aff)
    bump("draft_bio_ok" if bio else "draft_bio_none")
    return json_response(HTTPStatus.OK, {"bio": bio})


@app.post("/api/event")
def api_event():
    try:
        kind = str(read_json().get("kind", ""))
    except json.JSONDecodeError:
        kind = ""
    if kind not in EVENT_KINDS:
        return json_response(HTTPStatus.BAD_REQUEST, {"error": "Unknown event kind."})
    bump(kind)
    return ("", HTTPStatus.NO_CONTENT, {"Cache-Control": "no-store"})


@app.get("/api/stats")
def api_stats():
    if not admin_ok(dashboard=True):
        return json_response(HTTPStatus.FORBIDDEN, {"error": "Invalid token."})
    # ?from=YYYY-MM-DD&to=YYYY-MM-DD (UTC calendar days, inclusive; the page pads a day each side and
    # regroups in its chosen zone). ?days=N still works. Capped at 90 days of buckets per call.
    day = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    to = request.args.get("to", "") if day.match(request.args.get("to", "")) else datetime.now(timezone.utc).date().isoformat()
    if day.match(request.args.get("from", "")):
        since = request.args["from"]
    else:
        days = max(1, min(90, int(request.args.get("days", "4") or 4)))
        since = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    since = max(since, (datetime.fromisoformat(to) - timedelta(days=90)).date().isoformat())
    until = to + "T24"  # string compare: everything bucketed on `to`
    with STORE.tx() as db:
        metrics = [dict(r) for r in db.execute("SELECT bucket, kind, n FROM metrics WHERE bucket >= %s AND bucket < %s ORDER BY bucket", (since, until)).fetchall()]
        # Derived from the edit ledger, not from browser beacons, so they count PEOPLE and CHANGES rather than
        # taps: first_claimed / first_created = a profile's earliest edit row (its first claim or creation —
        # one per person, ever); profile_edit = every later row (a real change to name/aff/bio/hobby/student).
        # Same {bucket, kind, n} shape as metrics so the chart, tiles and kinds table need no special casing.
        metrics += [dict(r) for r in db.execute("""
            WITH firsts AS (SELECT e.profile_id, p.person_id, min(e.created_at) AS first_at FROM profile_edits e
                            JOIN profiles p USING (profile_id) GROUP BY e.profile_id, p.person_id)  -- by profile only: a person_id can change across rows
            SELECT left(first_at, 16) AS bucket,
                   CASE WHEN person_id ~ '^[0-9]+$' THEN 'first_claimed' ELSE 'first_created' END AS kind, count(*) AS n
            FROM firsts WHERE first_at >= %s AND first_at < %s GROUP BY 1, 2
            UNION ALL
            SELECT left(e.created_at, 16), 'profile_edit', count(*)
            FROM profile_edits e JOIN firsts f ON f.profile_id = e.profile_id
            WHERE e.created_at > f.first_at AND e.created_at >= %s AND e.created_at < %s GROUP BY 1
            ORDER BY 1""", (since, until, since, until)).fetchall()]
        jobs = [dict(r) for r in db.execute(
            "SELECT left(requested_at, 13) AS hour, status, count(*) AS n FROM jobs WHERE requested_at >= %s AND requested_at < %s GROUP BY 1, 2 ORDER BY 1",
            (since, until)).fetchall()]
        profiles = db.execute("SELECT count(*) AS n FROM profiles").fetchone()["n"]
        students = db.execute("SELECT count(*) AS n FROM profiles WHERE is_student = 1").fetchone()["n"]
        # The people list for the dashboard (newest first). "claimed" = an attendee from the programme
        # (numeric Person ID); "created" = a brand-new signup (random id). ponytail: the id shape is the tell.
        # "when" = first seen (earliest edit row, i.e. the claim/creation); "edited" = last change if any
        # later edit happened, with how many — so a bio edit no longer floats someone up as a "new" signup.
        people = [{"name": r["name"], "aff": r["affiliation"], "when": r["first_at"] or r["updated_at"],
                   "edited": r["updated_at"] if r["edits"] > 1 else None, "edits": max(r["edits"] - 1, 0),
                   "kind": "claimed" if (r["person_id"] or "").isdigit() else "created",
                   "student": bool(r["is_student"]), "hobby": (r["side_interest"] or "")[:80]}
                  for r in db.execute("""SELECT p.name, p.affiliation, p.person_id, p.is_student, p.side_interest, p.updated_at,
                                                (SELECT min(created_at) FROM profile_edits e WHERE e.profile_id=p.profile_id) AS first_at,
                                                (SELECT count(*) FROM profile_edits e WHERE e.profile_id=p.profile_id) AS edits
                                         FROM profiles p ORDER BY first_at DESC NULLS LAST, p.updated_at DESC LIMIT 2000""").fetchall()]
    return json_response(HTTPStatus.OK, {"since": since, "until": to, "metrics": metrics, "jobs": jobs, "profilesTotal": profiles, "studentsTotal": students, "people": people,
                                         "generatedAt": utcnow()})


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
                subject="[CHI Peapod] Attendees are seeing a load error",
                body=(
                    "A browser reported it could not load the app's data (CSVs/embeddings) "
                    f"at all — attendees are seeing the 'Error! :(' screen.\n\nDetail:\n{message}\n\n"
                    "Check GET /api/health and that Nginx/CloudFront are serving authors.csv "
                    "and umap_positions.json normally."
                ),
            )
    return json_response(HTTPStatus.OK, {"ok": True})

def side_refit_due() -> bool:
    """True if the side space has never been fitted or its last fit is older than the interval."""
    path = ROOT / "side_umap_positions.json"
    if not path.exists():
        # Never fitted; embed_umap.py exits early if too few people have opted in, so this is cheap.
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
    # Non-daemon timers would hold a gunicorn worker open after SIGTERM (~30 s downtime per restart).
    t = threading.Timer(delay, fn)
    t.daemon = True
    t.start()


def periodic_side_refit_check() -> None:
    """Every SIDE_REFIT_CHECK_SECONDS: refit the Hobby space if due. Skips the cycle when a user-driven
    job is queued or running."""
    try:
        current = STORE.current_job()
        if current and current["status"] in ("queued", "running"):
            return
        if side_refit_due():
            start_rebuild(kind="side_only")
    finally:
        _daemon_timer(SIDE_REFIT_CHECK_SECONDS, periodic_side_refit_check)


def new_feed_people() -> list[dict] | None:
    """People in authors.csv with no embedding yet; None when nothing is embedded at all (needs a full fit)."""
    ids_path = ROOT / "embeddings_person_ids.json"
    if not ids_path.exists():
        return None
    embedded = set(json.loads(ids_path.read_text()))
    with open(ROOT / "authors.csv", encoding="utf-8-sig", newline="") as f:
        return [{"personId": r["Person ID"], "profileId": r["Person ID"], "name": r["Author"],
                 "bio": r["s_profile"] or r["Interest Profile"] or r["Author"], "sideInterest": r.get("Side Interest", "")}
                # Chairs with no papers have no text to place.
                for r in csv.DictReader(f) if r.get("Person ID") and r["Person ID"] not in embedded
                and (r["s_profile"] or r["Interest Profile"])]


def periodic_feed_check() -> None:
    """Every FEED_CHECK_SECONDS: on a new feed revision, rewrite the CSVs and place only new people.
    Existing dots never move; changed abstracts wait for a deliberate full run."""
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
        # A feed outage only logs; the last good files keep serving.
        print(f"periodic_feed_check: {exc.__class__.__name__}: {exc}", flush=True)
    finally:
        _daemon_timer(FEED_CHECK_SECONDS, periodic_feed_check)


def recover_jobs() -> None:
    """After a restart: fail the job that was running, re-arm the timer for a queued one."""
    job = STORE.current_job()
    if not job:
        return
    if job["status"] == "running":
        # Nothing will ever finish it, and a stuck "running" row blocks queue_rebuild forever.
        with STORE.tx() as db:
            db.execute("UPDATE jobs SET status='failed',finished_at=%s,detail=%s WHERE job_id=%s",
                (utcnow(), "Interrupted by service restart.", job["job_id"]))
        return
    if job["status"] == "queued" and REBUILD_DELAY_SECONDS:
        requested = datetime.fromisoformat(job["requested_at"])
        elapsed = (datetime.now(timezone.utc) - requested).total_seconds()
        _arm_timer(job["job_id"], REBUILD_DELAY_SECONDS - elapsed)


def start_background() -> None:
    """Once per process: recover jobs, then start the periodic timers."""
    recover_jobs()
    if SIDE_REFIT_CHECK_SECONDS:
        _daemon_timer(SIDE_REFIT_INITIAL_DELAY_SECONDS, periodic_side_refit_check)
    if FEED_CHECK_SECONDS:
        _daemon_timer(5, periodic_feed_check)


def main():
    """Local development only; production runs gunicorn."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    start_background()
    print(f"CHI Peapod API listening on {args.host}:{args.port}", flush=True)
    app.run(host=args.host, port=args.port, threaded=True, debug=False)

if __name__ == "__main__":
    main()

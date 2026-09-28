#!/usr/bin/env python3
"""Assert-based check of the rebuild queue/recovery logic in server.py.

Runs against a throwaway state dir/DB — no HTTP server, no model, no network.
"""
import io
import json
import os
import shutil
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import psycopg

STATE_DIR = tempfile.mkdtemp()
os.environ["HFES_STATE_DIR"] = STATE_DIR
# A throwaway database per run (needs a Postgres role with CREATEDB; peer auth locally).
TEST_DB = f"hfes_selftest_{os.getpid()}"
with psycopg.connect("dbname=postgres", autocommit=True) as _admin:
    _admin.execute(f'CREATE DATABASE "{TEST_DB}"')
os.environ["HFES_DATABASE_URL"] = f"dbname={TEST_DB}"
os.environ["HFES_AUTO_REBUILD_SECONDS"] = "900"

import server  # noqa: E402  (must set env vars first; server.py reads them at import time)


def test_queue_coalesces_while_queued():
    r1 = server.queue_rebuild({"profileId": "p1", "personId": None, "name": "Edit 1", "bio": "bio 1"})
    r2 = server.queue_rebuild({"profileId": "p2", "personId": None, "name": "Edit 2", "bio": "bio 2"})
    assert r1["jobId"] == r2["jobId"], "a second edit before the timer fires should reuse the same queued job"
    assert r2["coalesced"] is True
    with server.STORE.tx() as db:
        row = db.execute("SELECT detail FROM jobs WHERE job_id=%s", (r2["jobId"],)).fetchone()
    import json
    pending = json.loads(row["detail"])
    assert [p["name"] for p in pending] == ["Edit 1", "Edit 2"], "both edits should be pending, not overwritten"
    server.REBUILD_TIMER.cancel()


def test_queue_dedupes_same_person():
    with server.STORE.tx() as db:
        db.execute("DELETE FROM jobs")
    server.queue_rebuild({"profileId": "p1", "personId": "P1", "name": "Old Name", "bio": "old"})
    r2 = server.queue_rebuild({"profileId": "p1", "personId": "P1", "name": "New Name", "bio": "new"})
    with server.STORE.tx() as db:
        row = db.execute("SELECT detail FROM jobs WHERE job_id=%s", (r2["jobId"],)).fetchone()
    import json
    pending = json.loads(row["detail"])
    assert len(pending) == 1, "editing the same person twice in the quiet window should place them once"
    assert pending[0]["name"] == "New Name", "the later edit's text should win"
    server.REBUILD_TIMER.cancel()


def test_queue_while_running_gets_followup_job():
    with server.STORE.tx() as db:
        db.execute("DELETE FROM jobs")
    job_id = "running-job"
    with server.STORE.tx() as db:
        db.execute(
            "INSERT INTO jobs(job_id,status,requested_at,started_at) VALUES(%s,%s,%s,%s)",
            (job_id, "running", server.utcnow(), server.utcnow()))
    r = server.queue_rebuild({"profileId": "p3", "personId": None, "name": "Edit during running build", "bio": ""})
    assert r["jobId"] != job_id, "an edit arriving mid-rebuild must get its own follow-up job, not be dropped"
    assert r["coalesced"] is False
    if server.REBUILD_TIMER:
        server.REBUILD_TIMER.cancel()


def test_legacy_plain_string_detail_does_not_crash():
    """A job queued by a pre-upgrade process has detail as a plain reason string
    ("profile changed"), not the JSON pending-list this version writes. Coalescing into
    it, or a worker reading it, must not crash on json.loads — see _merge_pending and
    start_rebuild's worker."""
    with server.STORE.tx() as db:
        db.execute("DELETE FROM jobs")
        db.execute(
            "INSERT INTO jobs(job_id,status,requested_at,detail) VALUES(%s,%s,%s,%s)",
            ("legacy-job", "queued", server.utcnow(), "profile changed"))
    r = server.queue_rebuild({"profileId": "p4", "personId": None, "name": "New Edit", "bio": "bio"})
    assert r["jobId"] == "legacy-job" and r["coalesced"] is True
    import json
    with server.STORE.tx() as db:
        row = db.execute("SELECT detail FROM jobs WHERE job_id='legacy-job'").fetchone()
    pending = json.loads(row["detail"])
    assert [p["name"] for p in pending] == ["New Edit"], "legacy string detail should be treated as an empty pending list, then merged into"
    server.REBUILD_TIMER.cancel()


def test_routes_via_flask_test_client():
    assert server.app.debug is False
    c = server.app.test_client()
    r = c.get("/api/matches?limit=zzz")
    assert r.status_code == 400 and b"Traceback" not in r.data
    assert c.get("/api/health").status_code == 200
    assert c.get("/api/health").headers["Cache-Control"] == "no-store"
    assert c.get("/api/matches?offset=abc").status_code == 400
    assert c.get("/api/nope").status_code == 404
    assert c.get("/api/profile").status_code == 404
    assert c.post("/api/profile", data="[1,2]", content_type="application/json").status_code == 400
    assert c.post("/api/profile", data="x" * (server.MAX_BODY + 1), content_type="application/json").status_code == 400
    assert c.post("/api/admin/rebuild").status_code == 403
    r = c.post("/api/profile", data=json.dumps({"name": "Route Test", "aff": "X", "bio": "hello"}), content_type="application/json")
    assert r.status_code == 200 and r.get_json()["profile"]["name"] == "Route Test"
    assert r.get_json()["embeddingRefresh"]["queued"] is True


def test_recover_marks_stale_running_as_failed():
    with server.STORE.tx() as db:
        db.execute("DELETE FROM jobs")
        db.execute(
            "INSERT INTO jobs(job_id,status,requested_at,started_at) VALUES(%s,%s,%s,%s)",
            ("stale", "running", server.utcnow(), server.utcnow()))
    server.recover_jobs()
    job = server.STORE.current_job()
    assert job["status"] == "failed", "a 'running' row surviving a restart has no process behind it anymore"


def test_upsert_profile_stores_is_student():
    row = server.STORE.upsert_profile({"profileId": "stu1", "name": "Stu Dent", "bio": "bio", "isStudent": True})
    assert row["is_student"] == 1, "isStudent:true in the POST body should persist as is_student=1"
    profile = server.row_json(row)
    assert profile["isStudent"] is True, "row_json should surface is_student back out as a bool"
    row2 = server.STORE.upsert_profile({"profileId": "stu1", "name": "Stu Dent", "bio": "bio", "isStudent": False})
    assert row2["is_student"] == 0, "re-saving with isStudent:false should clear the flag, not leave it sticky"


def test_client_ip_prefers_cloudfront_hop():
    h = {"X-Forwarded-For": "1.2.3.4, 10.0.0.9, 203.0.113.7", "X-Real-IP": "130.176.0.1"}
    assert server.client_ip(h, "127.0.0.1") == "203.0.113.7", "last XFF hop is the one CloudFront appended (viewer IP)"
    assert server.client_ip({"X-Real-IP": "198.51.100.2"}, "127.0.0.1") == "198.51.100.2", "no CloudFront hop: X-Real-IP is the real client"
    assert server.client_ip({}, "127.0.0.1") == "127.0.0.1"


def test_profile_write_limit_is_sliding_window():
    server._profile_writes.clear()
    t0 = 1000.0
    for i in range(server.PROFILE_WRITES_PER_WINDOW):
        assert server.profile_write_allowed("9.9.9.9", t0 + i), f"write {i+1} within the cap must be allowed"
    assert not server.profile_write_allowed("9.9.9.9", t0 + 30), "the 21st write inside the window is refused"
    assert server.profile_write_allowed("8.8.8.8", t0 + 30), "another IP has its own bucket"
    assert server.profile_write_allowed("9.9.9.9", t0 + server.PROFILE_WINDOW_SECONDS + 1), "once the oldest writes age out, allowed again"


def test_match_page_student_only_filters():
    embedding_index = server.CATALOGUE._spaces["main"]["embedding_index"]
    if not embedding_index:
        return  # no embeddings.npy in this checkout — nothing to filter
    person_id = next(iter(embedding_index))
    all_matches, all_total = server.CATALOGUE.match_page(person_id, 0, 50, student_only=False)
    student_matches, student_total = server.CATALOGUE.match_page(person_id, 0, 50, student_only=True)
    assert student_total <= all_total, "filtering to students-only must never return more than the unfiltered total"
    assert all(m.get("isStudent") for m in student_matches), "studentOnly=1 must not leak a non-student into the page"


def test_match_page_side_space_empty_before_first_fit():
    # The side space's files don't exist until enough people have opted in and a full rebuild
    # has run — Catalogue.reload() must treat that as "zero people embedded there", not crash.
    matches, total = server.CATALOGUE.match_page("nobody-yet", 0, 50, space="side")
    assert matches == [] and total == 0, "an unfitted side space should look like an empty result, not raise"


def test_upsert_profile_stores_side_interest():
    row = server.STORE.upsert_profile({"profileId": "side1", "name": "Side Person", "bio": "bio", "sideInterest": "fountain pens"})
    assert row["side_interest"] == "fountain pens"
    profile = server.row_json(row)
    assert profile["sideInterest"] == "fountain pens", "row_json should surface side_interest back out"
    row2 = server.STORE.upsert_profile({"profileId": "side1", "name": "Side Person", "bio": "bio", "sideInterest": ""})
    assert row2["side_interest"] == "", "re-saving with an empty sideInterest should clear it, not leave it sticky"


def test_draft_bio_noop_without_api_key():
    # The realistic condition for local dev/tests, and the required behavior in production if the
    # key is ever unset/revoked: never attempt a network call, never raise, just decline the draft.
    assert server.ANTHROPIC_API_KEY == "", "test env must not have a real key set"
    assert server.draft_bio("Jane Doe", "Test University") is None


def test_send_alert_email_noop_without_config():
    # Same pattern as draft_bio: unconfigured must mean silently do nothing, never raise, never
    # attempt a network call — this fires from inside the rebuild worker's exception handler, so it
    # must never itself throw and mask the original failure.
    assert not (server.ALERT_EMAIL_TO and server.ALERT_SMTP_USER and server.ALERT_SMTP_PASSWORD), \
        "test env must not have real alert email config set"
    server.send_alert_email("test subject", "test body")  # must not raise


def test_send_alert_email_swallows_send_failure():
    # A misconfigured or unreachable SMTP server must not crash the caller (the rebuild worker) —
    # confirmed by pointing at a config that's present but guaranteed to fail to connect.
    with mock.patch.object(server, "ALERT_EMAIL_TO", "test@example.com"), \
         mock.patch.object(server, "ALERT_SMTP_USER", "test@example.com"), \
         mock.patch.object(server, "ALERT_SMTP_PASSWORD", "fake"), \
         mock.patch.object(server, "ALERT_SMTP_HOST", "127.0.0.1"), \
         mock.patch.object(server, "ALERT_SMTP_PORT", 1):  # port 1 is never a real SMTP listener
        server.send_alert_email("test subject", "test body")  # must not raise


class _FakeAnthropicResponse:
    """Just enough of urllib's response object for draft_bio(): .read() plus context-manager protocol."""
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode()
    def read(self):
        return self._payload
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False


def test_draft_bio_rejects_no_match_even_with_preamble():
    # Regression: confirmed live against the real API during testing — Haiku sometimes narrates its
    # search before the sentinel ("I'll search for information about this person... NO_MATCH")
    # instead of replying with only NO_MATCH as instructed. An exact-string check missed this and
    # would have handed that narration to a real attendee as their "bio."
    fake = _FakeAnthropicResponse({
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": "I'll search for information about this person.\n\nNO_MATCH"}],
    })
    with mock.patch.object(server, "ANTHROPIC_API_KEY", "fake-key-for-test"), \
         mock.patch("urllib.request.urlopen", return_value=fake):
        assert server.draft_bio("Nobody Real", "Nowhere University") is None


def test_draft_bio_returns_clean_bio_text():
    fake = _FakeAnthropicResponse({
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": "Dr. Real Person studies human factors and ergonomics."}],
    })
    with mock.patch.object(server, "ANTHROPIC_API_KEY", "fake-key-for-test"), \
         mock.patch("urllib.request.urlopen", return_value=fake):
        assert server.draft_bio("Real Person", "Some University") == "Dr. Real Person studies human factors and ergonomics."


def test_draft_bio_strips_leading_artifact():
    # Regression: confirmed live — even the final text block can start with a stray leftover
    # fragment (e.g. ".\n\n") from a truncated lead-in sentence, ahead of the real bio.
    fake = _FakeAnthropicResponse({
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": ".\n\nDr. Real Person studies human factors."}],
    })
    with mock.patch.object(server, "ANTHROPIC_API_KEY", "fake-key-for-test"), \
         mock.patch("urllib.request.urlopen", return_value=fake):
        assert server.draft_bio("Real Person", "Some University") == "Dr. Real Person studies human factors."


def test_draft_bio_ignores_interim_search_narration():
    # Regression: confirmed live against the real API — without extended thinking, Haiku's reasoning
    # between search rounds lands in separate visible text blocks ("I'll search...", "Not found, let
    # me try again...") ahead of a final text block with the real bio. Joining every text block
    # together (an earlier version of this fix) concatenated that narration onto the front of the
    # real bio and returned the whole thing as the attendee's "profile." Only text blocks after the
    # last tool-related block should count.
    fake = _FakeAnthropicResponse({
        "stop_reason": "end_turn",
        "content": [
            {"type": "text", "text": "I'll search for information about this person."},
            {"type": "tool_use", "id": "1", "name": "web_search", "input": {}},
            {"type": "web_search_tool_result", "tool_use_id": "1", "content": []},
            {"type": "text", "text": "Not found, let me try a more specific search."},
            {"type": "tool_use", "id": "2", "name": "web_search", "input": {}},
            {"type": "web_search_tool_result", "tool_use_id": "2", "content": []},
            {"type": "text", "text": "Perfect, found them.\n\nDr. Real Person studies human factors."},
        ],
    })
    with mock.patch.object(server, "ANTHROPIC_API_KEY", "fake-key-for-test"), \
         mock.patch("urllib.request.urlopen", return_value=fake):
        result = server.draft_bio("Real Person", "Some University")
    assert result is not None and "I'll search" not in result and "Not found" not in result, \
        f"interim narration leaked into the returned bio: {result!r}"


def test_draft_bio_joins_split_final_answer():
    # Regression: confirmed live against the real API — the two-line answer (bolded headline, then
    # one sentence) is sometimes split across two separate text blocks even after all tool use is
    # done. Taking only the very last block (an earlier version of this fix) silently dropped the
    # headline half of a correctly-formatted answer.
    fake = _FakeAnthropicResponse({
        "stop_reason": "end_turn",
        "content": [
            {"type": "thinking", "thinking": "..."},
            {"type": "server_tool_use", "id": "1", "name": "web_search", "input": {}},
            {"type": "web_search_tool_result", "tool_use_id": "1", "content": []},
            {"type": "text", "text": "**Human-Automation Interaction and Trust**\n"},
            {"type": "text", "text": "Investigates trust in automation and human-AI teaming."},
        ],
    })
    with mock.patch.object(server, "ANTHROPIC_API_KEY", "fake-key-for-test"), \
         mock.patch("urllib.request.urlopen", return_value=fake):
        result = server.draft_bio("Real Person", "Some University")
    assert result == "**Human-Automation Interaction and Trust**\nInvestigates trust in automation and human-AI teaming.", \
        f"headline block was dropped: {result!r}"


def test_draft_bio_strips_preamble_before_headline():
    # Regression: confirmed live against the real API — a whole preamble sentence ("Based on my
    # search results, I found that...") can precede the correctly-formatted answer with no tool-call
    # boundary in between, so the block-splitting logic above can't separate them. The bolded headline
    # line is the tell: real narration never uses that markdown.
    fake = _FakeAnthropicResponse({
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": (
            "Based on my search results, I found that Real Person holds a PhD from Some University.\n\n"
            "**Human factors and cognitive load**\nStudies attention under fatigue."
        )}],
    })
    with mock.patch.object(server, "ANTHROPIC_API_KEY", "fake-key-for-test"), \
         mock.patch("urllib.request.urlopen", return_value=fake):
        result = server.draft_bio("Real Person", "Some University")
    assert result == "**Human factors and cognitive load**\nStudies attention under fatigue.", \
        f"preamble leaked ahead of the headline: {result!r}"


def test_recover_rearms_queued_timer():
    with server.STORE.tx() as db:
        db.execute("DELETE FROM jobs")
        requested = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
        db.execute(
            "INSERT INTO jobs(job_id,status,requested_at) VALUES(%s,%s,%s)",
            ("re-armed", "queued", requested))
    server.recover_jobs()
    assert server.REBUILD_TIMER is not None
    assert server.REBUILD_TIMER.is_alive(), "a queued job from before a restart must get a fresh timer, not sit inert"
    server.REBUILD_TIMER.cancel()


def test_side_refit_due_when_never_fitted():
    tmp = Path(tempfile.mkdtemp())
    try:
        with mock.patch.object(server, "ROOT", tmp):
            assert server.side_refit_due() is True, "no side_umap_positions.json at all means a refit is due"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_side_refit_due_respects_interval():
    tmp = Path(tempfile.mkdtemp())
    path = tmp / "side_umap_positions.json"
    try:
        with mock.patch.object(server, "ROOT", tmp):
            fresh = datetime.now(timezone.utc).isoformat()
            path.write_text(json.dumps({"generated_at": fresh, "people": []}))
            assert server.side_refit_due() is False, "a refit that just happened should not be due again immediately"

            old = (datetime.now(timezone.utc) - timedelta(seconds=server.SIDE_REFIT_INTERVAL_SECONDS + 10)).isoformat()
            path.write_text(json.dumps({"generated_at": old, "people": []}))
            assert server.side_refit_due() is True, "a refit older than the interval should be due again"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_side_only_sentinel_is_not_valid_json():
    # Documents exactly the bug class the worker's `detail_raw == "side_only"` check (checked
    # BEFORE any json.loads attempt) avoids: "side_only" isn't valid JSON, so a naive try/except
    # around json.loads() alone would silently swallow it into the same fallback as a legacy
    # plain-string detail -- running a full BOTH-spaces rebuild instead of the side-only one that
    # was actually requested.
    try:
        json.loads("side_only")
        raise AssertionError('"side_only" should not be valid JSON')
    except json.JSONDecodeError:
        pass


def test_start_rebuild_side_only_runs_the_right_subprocess():
    with server.STORE.tx() as db:
        db.execute("DELETE FROM jobs")
    fake_result = mock.Mock(returncode=0, stdout="", stderr="")
    with mock.patch.object(server.subprocess, "run", return_value=fake_result) as mock_run:
        result = server.start_rebuild(kind="side_only")
        assert result["started"] is True, "expected the lock to be free and a job to start"
        row = None
        for _ in range(50):
            with server.STORE.tx() as db:
                row = db.execute(
                    "SELECT status,detail FROM jobs WHERE job_id=%s", (result["jobId"],)).fetchone()
            if row["status"] in ("complete", "failed"):
                break
            time.sleep(0.05)
    assert row["status"] == "complete", f"worker should complete against the mocked subprocess: {row['detail'] if row else None}"
    assert row["detail"] == "Periodic Hobby-space refit complete (academic map untouched)."
    invoked_args = mock_run.call_args.args[0]
    assert "--side-only" in invoked_args, "a side_only job must invoke embed_umap.py full --side-only, not a plain full"


def test_periodic_side_refit_check_skips_when_a_real_job_is_queued():
    # Must never compete with a real edit's rebuild -- if one is queued or running, this check
    # should do nothing at all this cycle (no new job, no call to start_rebuild).
    with server.STORE.tx() as db:
        db.execute("DELETE FROM jobs")
        db.execute("INSERT INTO jobs(job_id,status,requested_at,detail) VALUES(%s,%s,%s,%s)",
            ("real-edit-job", "queued", server.utcnow(), "[]"))
    with mock.patch.object(server, "start_rebuild") as mock_start, \
         mock.patch.object(server.threading, "Timer") as mock_timer:
        server.periodic_side_refit_check()
    mock_start.assert_not_called()
    mock_timer.assert_called_once()  # still reschedules itself even when it skips


if __name__ == "__main__":
    try:
        test_queue_coalesces_while_queued()
        test_queue_dedupes_same_person()
        test_queue_while_running_gets_followup_job()
        test_legacy_plain_string_detail_does_not_crash()
        test_upsert_profile_stores_is_student()
        test_upsert_profile_stores_side_interest()
        test_match_page_student_only_filters()
        test_match_page_side_space_empty_before_first_fit()
        test_draft_bio_noop_without_api_key()
        test_send_alert_email_noop_without_config()
        test_send_alert_email_swallows_send_failure()
        test_draft_bio_rejects_no_match_even_with_preamble()
        test_draft_bio_returns_clean_bio_text()
        test_draft_bio_strips_leading_artifact()
        test_draft_bio_ignores_interim_search_narration()
        test_draft_bio_joins_split_final_answer()
        test_draft_bio_strips_preamble_before_headline()
        test_routes_via_flask_test_client()
        test_recover_marks_stale_running_as_failed()
        test_recover_rearms_queued_timer()
        test_side_refit_due_when_never_fitted()
        test_side_refit_due_respects_interval()
        test_side_only_sentinel_is_not_valid_json()
        test_start_rebuild_side_only_runs_the_right_subprocess()
        test_periodic_side_refit_check_skips_when_a_real_job_is_queued()
        print("selftest OK")
    finally:
        shutil.rmtree(STATE_DIR, ignore_errors=True)
        server.STORE.conn.close()
        with psycopg.connect("dbname=postgres", autocommit=True) as _admin:
            _admin.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)')

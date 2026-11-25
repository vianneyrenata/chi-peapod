#!/usr/bin/env python3
"""Pull the CHI programme feed and write authors.csv, program.csv and embed_text.json.

Polls version.json; fetches the immutable revision_url only when `revision` moves.
    python3 chi_feed.py [--edition chi2026|sandbox|chi2027] [--force]
"""
import argparse
import csv
import io
import html
import json
import os
import re
import unicodedata
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent
FEED_BASE = "https://chidigitalexperience.org/data"
EDITION = os.environ.get("HFES_FEED_EDITION", "chi2026")  # switch to chi2027 once it's live (April 2027)
REVISION_PATH = ROOT / "feed_revision.json"
EMBED_TEXT_PATH = ROOT / "embed_text.json"
# MiniLM truncates at 256 word pieces anyway (~190 words); more text only costs time.
EMBED_CHARS = 1500

AUTHOR_FIELDS = ["Author", "Affiliation", "Person ID", "Schedule Detail", "Interest Profile",
                 "Presentation Titles", "s_profile", "Is Student", "Side Interest"]
PROGRAM_FIELDS = ["Date", "Day", "Start Time", "End Time", "Location", "Session Name", "Session Type",
                  "Presentation Title", "Authors", "Presenting Author(s)", "Session Chair(s)",
                  "Presentation ID", "Session ID", "Program URL"]


def _get_json(url: str, timeout: float = 60):
    req = urllib.request.Request(url, headers={"User-Agent": "chi-peapod (programme sync)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _clean(s) -> str:
    # " · " and "|" are Schedule Detail's separators and "[...]" ends it (see SCHEDULE_RE in
    # index.html); a title containing them would silently fail to parse.
    s = html.unescape(str(s or ""))                       # sandbox has "&amp;" / "&lt;" in a title
    s = re.sub(r"[\ufeff\u200b-\u200f]", "", s)             # BOM / zero-width marks inside names
    s = s.lstrip("".join(c for c in s[:3] if unicodedata.category(c).startswith("M")))  # stray leading diacritic
    return re.sub(r"\s+", " ", s).replace("·", "-").replace("|", "/").replace("[", "(").replace("]", ")").strip()


def _clock(dt: datetime) -> str:
    return dt.strftime("%I:%M %p").lstrip("0").lower()  # "1:50 pm", the format the app parses


def person_name(p: dict) -> str:
    return _clean(" ".join(x for x in (p.get("firstName"), p.get("middleInitial"), p.get("lastName")) if x))


def convert(feed: dict, edition: str = "") -> tuple[list[dict], list[dict], dict]:
    """Pure: feed JSON -> (author rows, program rows, embed_text). No I/O, so it's testable."""
    # Feed epochs are local wall-clock stored as UTC (CHI 2025's 8:45 plenary is 08:45Z): format in UTC.
    tz = timezone.utc
    people = {p["id"]: p for p in feed["people"]}
    rooms = {r["id"]: _clean(r["name"]) for r in feed.get("rooms", [])}
    slots = {t["id"]: t for t in feed.get("timeSlots", [])}
    types = {t["id"]: t["name"] for t in feed.get("contentTypes", [])}
    contents = {c["id"]: c for c in feed["contents"]}

    sched: dict[int, list[str]] = {}      # person id -> Schedule Detail entries
    titles: dict[int, list[str]] = {}
    abstracts: dict[int, list[str]] = {}
    affs: dict[int, list[str]] = {}
    program = []
    site = re.fullmatch(r"([a-z]+)(\d{4})", edition)

    for c in feed["contents"]:
        if c.get("isBreak"):
            continue
        for a in (c.get("authors") or []):
            pid = a["personId"]
            titles.setdefault(pid, []).append(_clean(c["title"]))
            abstracts.setdefault(pid, []).append(_clean(c.get("abstract")))
            for af in (a.get("affiliations") or []):
                inst = _clean(af.get("institution"))
                if inst and inst not in affs.setdefault(pid, []):
                    affs[pid].append(inst)

    for s in feed["sessions"]:
        slot = slots.get(s.get("timeSlotId"))
        if not slot:
            continue  # unscheduled sessions can't be placed on the programme
        start = datetime.fromtimestamp(slot["startDate"] / 1000, tz)
        end = datetime.fromtimestamp(slot["endDate"] / 1000, tz)
        date, times = start.strftime("%Y-%m-%d"), f"{_clock(start)}-{_clock(end)}"
        room, sname = rooms.get(s.get("roomId")) or "TBA", _clean(s["name"])
        # No status field in the feed; a withdrawn session is addons.cancelled. Label it, don't hide it.
        if (s.get("addons") or {}).get("cancelled") and "cancel" not in sname.lower():
            sname = "CANCELLED: " + sname
        chairs = [people[i] for i in (s.get("chairIds") or []) if i in people]
        for i in (s.get("chairIds") or []):
            sched.setdefault(i, []).append(f"{date} {times} · {room} · {sname} · {sname} [Session Chair]")
        for cid in (s.get("contentIds") or []):
            c = contents.get(cid)
            if not c or c.get("isBreak"):
                continue
            title = _clean(c["title"])
            names = [person_name(people[a["personId"]]) for a in (c.get("authors") or []) if a["personId"] in people]
            for a in (c.get("authors") or []):
                sched.setdefault(a["personId"], []).append(f"{date} {times} · {room} · {sname} · {title} [Author]")
            # DOI when there is one, else the item's page on programs.sigchi.org (sandbox has none)
            doi = ((c.get("addons") or {}).get("doi") or {}).get("url", "") or (
                f"https://programs.sigchi.org/{site.group(1)}/{site.group(2)}/program/content/{cid}" if site else "")
            program.append({
                "Date": date, "Day": start.strftime("%A"), "Start Time": _clock(start), "End Time": _clock(end),
                "Location": "" if room == "TBA" else room, "Session Name": sname,
                "Session Type": types.get(c.get("typeId"), ""), "Presentation Title": title,
                "Authors": "; ".join(names), "Presenting Author(s)": "",
                "Session Chair(s)": "; ".join(person_name(p) for p in chairs),
                "Presentation ID": str(cid), "Session ID": str(s["id"]), "Program URL": doi,
            })

    authors, embed_text = [], {}
    for pid in sorted(set(titles) | set(sched)):
        if pid not in people:
            continue
        own = list(dict.fromkeys(titles.get(pid, [])))  # dedupe, keep order
        display = ("**At " + feed["conference"].get("name", "CHI") + "**\n" + "\n".join(own)) if own else ""
        text = " ".join(own + [a for a in abstracts.get(pid, []) if a])[:EMBED_CHARS]
        authors.append({
            "Author": person_name(people[pid]), "Affiliation": "; ".join(affs.get(pid, [])),
            "Person ID": str(pid), "Schedule Detail": " | ".join(dict.fromkeys(sched.get(pid, []))),
            "Interest Profile": display, "Presentation Titles": "; ".join(own),
            "s_profile": "", "Is Student": "", "Side Interest": "",
        })
        if text:
            embed_text[str(pid)] = [display, text]
    return authors, program, embed_text


def _csv(rows: list[dict], fields: list[str]) -> str:
    sio = io.StringIO()
    w = csv.DictWriter(sio, fieldnames=fields)
    w.writeheader()
    w.writerows(rows)
    return sio.getvalue()


def sync(edition: str = EDITION, force: bool = False) -> dict:
    """Fetch + write only when the feed's revision moved. Returns {changed, revision, people}.
    Callers that share files with embed_umap.py (server.py) must hold REBUILD_LOCK around this."""
    from embed_umap import _atomic_write_text, materialize_db_edits
    version = _get_json(f"{FEED_BASE}/{edition}/version.json", timeout=20)
    seen = json.loads(REVISION_PATH.read_text()) if REVISION_PATH.exists() else {}
    key = [version["edition"], version["revision"]]
    if not force and seen.get("key") == key:
        return {"changed": False, "revision": version["revision"]}
    authors, program, embed_text = convert(_get_json(version["revision_url"]), version["edition"])
    _atomic_write_text(ROOT / "program.csv", _csv(program, PROGRAM_FIELDS))
    _atomic_write_text(EMBED_TEXT_PATH, json.dumps(embed_text))
    _atomic_write_text(ROOT / "authors.csv", _csv(authors, AUTHOR_FIELDS))
    # the rewrite just dropped every claimed edit and signup; put them back from the database
    try:
        materialize_db_edits()
    except Exception as exc:  # no database (local one-off run) — the next job re-applies them anyway
        print(f"chi_feed: edits not re-applied ({exc.__class__.__name__}); the next job will", flush=True)
    _atomic_write_text(REVISION_PATH, json.dumps({"key": key, "generated_at": version.get("generated_at"),
                                                  "synced_at": datetime.now().astimezone().isoformat()}))
    return {"changed": True, "revision": version["revision"], "people": len(authors), "program": len(program)}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--edition", default=EDITION)
    ap.add_argument("--force", action="store_true", help="rewrite even if the revision hasn't changed")
    print(json.dumps(sync(ap.parse_args().edition, ap.parse_args().force)))

"""python3 test_chi_feed.py — convert() on a feed seeded with the sandbox's edge cases."""
from datetime import datetime, timezone
from chi_feed import convert

MS = lambda *a: int(datetime(*a, tzinfo=timezone.utc).timestamp() * 1000)  # wall-clock stored as UTC

FEED = {
    "conference": {"name": "CHI 2027", "timeZoneName": "America/New_York"},
    "people": [{"id": 1, "firstName": "َAda", "lastName": "Lovelace", "middleInitial": None}],
    "rooms": [{"id": 7, "name": "Hall﻿ A"}], "contentTypes": [],
    "timeSlots": [{"id": 5, "startDate": MS(2027, 5, 11, 8, 45), "endDate": MS(2027, 5, 11, 10, 15)}],
    "contents": [{"id": 10, "title": "Cats &amp; &lt;Dogs&gt;", "abstract": None, "trackId": None,
                  "authors": [{"personId": 1, "affiliations": None}], "addons": {"doi": None}},
                 {"id": 11, "title": "Orphan", "authors": None}],
    "sessions": [{"id": 20, "name": "Withdrawn", "roomId": None, "chairIds": None, "contentIds": [10, 11],
                  "timeSlotId": 5, "addons": {"cancelled": True}}],
}

authors, program, embed = convert(FEED, "chi2027")
a, = authors
assert a["Author"] == "Ada Lovelace", a["Author"]
assert a["Presentation Titles"] == "Cats & <Dogs>", a["Presentation Titles"]
assert a["Schedule Detail"] == "2027-05-11 8:45 am-10:15 am · TBA · CANCELLED: Withdrawn · Cats & <Dogs> [Author]", a["Schedule Detail"]
assert [p["Presentation Title"] for p in program] == ["Cats & <Dogs>", "Orphan"]
assert program[0]["Start Time"] == "8:45 am" and program[0]["Location"] == "" and program[0]["Program URL"] == "https://programs.sigchi.org/chi/2027/program/content/10"
assert embed == {"1": ["**At CHI 2027**\nCats & <Dogs>", "Cats & <Dogs>"]}, embed
print("test_chi_feed OK")

"""Locust load test modelled on real attendee traffic against the API (static files are nginx/CloudFront).

  locust -f locustfile.py --host http://127.0.0.1:8000                           # web UI on :8089
  locust -f locustfile.py --host http://127.0.0.1:8000 --headless -u 100 -r 20 -t 60s

Read-only by default. HFES_LOCUST_WRITES=1 adds POST /api/profile — never against production:
writes upsert real Person IDs and land in the live attendee CSV.
"""
import json
import os
import random
from pathlib import Path
from urllib.parse import quote

from locust import HttpUser, between, task

PERSON_IDS = json.loads((Path(__file__).parent / "embeddings_person_ids.json").read_text())
WRITES = os.environ.get("HFES_LOCUST_WRITES") == "1"


class Attendee(HttpUser):
    wait_time = between(1, 5)

    @task(10)
    def show_more_matches(self):
        pid = quote(random.choice(PERSON_IDS))
        self.client.get(f"/api/matches?personId={pid}&offset=0&limit=50", name="/api/matches")

    @task(3)
    def poll_placement(self):
        pid = quote(random.choice(PERSON_IDS))
        self.client.get(f"/api/placement?personId={pid}", name="/api/placement")

    @task(1)
    def health(self):
        self.client.get("/api/health")

    @task(2 if WRITES else 0)
    def edit_profile(self):
        pid = random.choice(PERSON_IDS)
        self.client.post("/api/profile", name="/api/profile", json={
            "profileId": pid, "personId": pid, "name": "Load Test", "aff": "Load Test Co",
            "bio": f"load test bio {random.random()}"})

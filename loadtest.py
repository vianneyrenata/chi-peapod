#!/usr/bin/env python3
"""Concurrent load test against the HFES match API (stdlib only, no deps).

Real traffic mix, per RECOMPUTE.md: index.html/CSVs/JSON/npy are served by
Nginx directly and never touch this script. The only per-attendee API calls
are GET /api/matches ("show more matches") and POST /api/profile (an edit or
signup) -- both cheap (SQLite write + in-memory enqueue; the actual embedding
subprocess runs later, off the request path, so it is not what this measures).

Usage:
  python3 loadtest.py --base-url http://127.0.0.1:8000 --concurrency 50 --requests 500
  python3 loadtest.py --base-url https://<your-cloudfront-domain> --concurrency 50 --requests 500 --write-fraction 0
"""
import argparse
import json
import random
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PERSON_IDS = json.loads((ROOT / "embeddings_person_ids.json").read_text())


def one_request(base_url: str, write_fraction: float):
    is_write = random.random() < write_fraction
    pid = random.choice(PERSON_IDS)
    t0 = time.monotonic()
    try:
        if is_write:
            body = json.dumps({"profileId": pid, "personId": pid, "name": "Load Test",
                                "aff": "Load Test Co", "bio": f"load test bio {random.random()}"}).encode()
            req = urllib.request.Request(f"{base_url}/api/profile", data=body,
                                          headers={"Content-Type": "application/json"}, method="POST")
        else:
            req = urllib.request.Request(f"{base_url}/api/matches?personId={pid}&offset=0&limit=50")
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
            ok = resp.status == 200
    except Exception:
        ok = False
    return ok, time.monotonic() - t0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument("--concurrency", type=int, default=50)
    ap.add_argument("--requests", type=int, default=500)
    ap.add_argument("--write-fraction", type=float, default=0.05,
                     help="fraction of requests that are POST /api/profile edits (default 0.05)")
    args = ap.parse_args()

    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = [pool.submit(one_request, args.base_url, args.write_fraction) for _ in range(args.requests)]
        results = [f.result() for f in as_completed(futures)]

    latencies = sorted(r[1] for r in results)
    errors = sum(1 for r in results if not r[0])

    def pct(p):
        return latencies[min(len(latencies) - 1, int(len(latencies) * p))]

    print(f"{len(results)} requests, concurrency={args.concurrency}, errors={errors}")
    print(f"p50={pct(.50)*1000:.0f}ms  p95={pct(.95)*1000:.0f}ms  p99={pct(.99)*1000:.0f}ms  max={latencies[-1]*1000:.0f}ms")


if __name__ == "__main__":
    main()

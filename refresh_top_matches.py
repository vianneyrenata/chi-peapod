#!/usr/bin/env python3
"""Expand the cached neighbour lists without running the embedding model again."""
import json
import os
import tempfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).parent
TOP_K = 20
positions_path = ROOT / "umap_positions.json"
ids = json.loads((ROOT / "embeddings_person_ids.json").read_text())
embeddings = np.load(ROOT / "embeddings.npy")
payload = json.loads(positions_path.read_text())
names = {person["person_id"]: person["name"] for person in payload["people"]}

for i, person in enumerate(payload["people"]):
    order = np.argsort(-(embeddings @ embeddings[i]))
    person["top_matches"] = [
        {"person_id": ids[j], "name": names.get(ids[j], "?"), "score": round(float(embeddings[i] @ embeddings[j]) * 100)}
        for j in order if j != i
    ][:TOP_K]

with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=ROOT, delete=False) as output:
    json.dump(payload, output, indent=2)
    temp_name = output.name
os.replace(temp_name, positions_path)
print(f"Wrote {TOP_K} precomputed matches for {len(payload['people'])} attendees.")

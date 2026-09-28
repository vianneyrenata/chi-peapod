#!/usr/bin/env bash
set -euo pipefail
HOST="${1:?usage: deploy/deploy.sh ubuntu@host}"
KEY="${HFES_SSH_KEY:?set HFES_SSH_KEY to the EC2 key file}"
SSH=(ssh -i "$KEY" -o StrictHostKeyChecking=accept-new "$HOST")
RSYNC_RSH="ssh -i $KEY -o StrictHostKeyChecking=accept-new"

"${SSH[@]}" 'sudo mkdir -p /opt/hfes-match && sudo chown -R "$USER":"$USER" /opt/hfes-match'
# --delete without excluding uv's own install dirs (.local/.cache/.config) is destructive: none
# of those exist in the local source tree, so --delete tried to remove them on the server on
# 2026-08-23 — including .local/share/uv/python/..., the real interpreter directory .venv/bin/
# python symlinks to. Confirmed live: this broke the venv while the site stayed up on its
# already-running process, and the next subprocess spawn (an embedding rebuild job) would have
# failed. See CLAUDE.md for the incident and repair.
# The embedding/position artifacts and the attendee CSV are MUTATED ON THE SERVER at runtime
# (incremental placements append to embeddings.npy/umap_positions.json; materialize_db_edits()
# rewrites authors.csv from SQLite; the Hobby space refits itself daily). Pushing the
# local copies over them silently erased every signup's placement on each deploy — found
# 2026-09-02: 7 new attendees had been placed on 08-24 and wiped on 08-25, leaving them on
# name-hash placeholder dots with keyword-overlap matches. The server's copies are the source of
# truth; a deliberate new data export must be copied over by hand (then `embed_umap.py full`).
rsync -az --delete -e "$RSYNC_RSH" \
  --exclude '.git' --exclude 'state*' --exclude 'ec2-backup-*' --exclude '*.pdf' --exclude '.venv' --exclude '.python312' --exclude '__pycache__' \
  --exclude '.local' --exclude '.cache' --exclude '.config' \
  --exclude 'authors.csv' --exclude 'program.csv' --exclude 'embed_text.json' --exclude 'feed_revision.json' --exclude 'umap_positions.json' --exclude 'side_umap_positions.json' \
  --exclude 'embeddings.npy' --exclude 'side_embeddings.npy' \
  --exclude 'embeddings_person_ids.json' --exclude 'side_embeddings_person_ids.json' \
  --exclude 'umap_reducer.joblib' --exclude 'side_umap_reducer.joblib' \
  ./ "$HOST:/opt/hfes-match/"
"${SSH[@]}" 'sudo bash /opt/hfes-match/deploy/bootstrap-server.sh'

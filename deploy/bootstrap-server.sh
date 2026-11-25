#!/usr/bin/env bash
set -euo pipefail

# This script is invoked over SSH from whatever directory the ubuntu user's session starts in
# (typically /home/ubuntu, mode 0750, owned by ubuntu) — `sudo -u hfesmatch` below preserves that
# CWD rather than resetting it, even though it correctly sets $HOME to /opt/hfes-match. uv's
# config-file discovery walks upward from CWD looking for a project uv.toml, and hfesmatch can't
# even stat a directory it's not permitted into — confirmed live on 2026-08-23: this made every
# `sudo -u hfesmatch uv ...` call below fail with "failed to open file '/home/ubuntu/uv.toml':
# Permission denied" before uv did anything else. cd here once, to a directory hfesmatch actually
# owns, so nothing later in this script depends on the SSH session's own working directory.
cd /opt/hfes-match

# The small instance uses swap only as an OOM safety net during the serialized
# embedding/UMAP worker; normal web traffic remains memory-resident.
if ! swapon --show | grep -q .; then
  fallocate -l 1G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

id -u hfesmatch >/dev/null 2>&1 || useradd --system --home /opt/hfes-match --shell /usr/sbin/nologin hfesmatch
install -d -o hfesmatch -g hfesmatch -m 0750 /opt/hfes-match/state
# deploy.sh temporarily makes /opt/hfes-match writable by the SSH user. Repair
# ownership before package work, so a transient apt/dpkg lock cannot leave the
# API unable to read its catalogue or open SQLite and stuck in a restart loop.
chown -R hfesmatch:hfesmatch /opt/hfes-match
chmod 0750 /opt/hfes-match/state
# The embedding pipeline atomically replaces its JSON using a private temporary
# file. Make all public static assets readable before apt work too, not only at
# the end of this script, so an interrupted bootstrap cannot leave UMAP JSON
# unreadable by Nginx/CloudFront.
# .local/.cache are excluded here too, not just state/.venv: they're uv's own install dirs
# (the real Python interpreter .venv/bin/python symlinks into, plus uv itself). A blanket
# chmod 0644 doesn't skip symlinks but DOES walk into the real directories reachable through
# them, silently stripping the +x bit off python3.12 and uv/uvx -- confirmed live on
# 2026-08-23: this is exactly what broke the service after a from-scratch venv install here,
# not the install itself.
find /opt/hfes-match \( -path /opt/hfes-match/state -o -path /opt/hfes-match/.venv -o -path /opt/hfes-match/.local -o -path /opt/hfes-match/.cache \) -prune -o -type d -exec chmod 0755 {} +
find /opt/hfes-match \( -path /opt/hfes-match/state -o -path /opt/hfes-match/.venv -o -path /opt/hfes-match/.local -o -path /opt/hfes-match/.cache \) -prune -o -type f -exec chmod 0644 {} +

apt-get update
apt-get install -y nginx python3-numpy curl postgresql
# Peer auth: Linux user hfesmatch logs in as Postgres role hfesmatch over the local socket, no password.
sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='hfesmatch'" | grep -q 1 \
  || sudo -u postgres createuser hfesmatch
sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='hfes'" | grep -q 1 \
  || sudo -u postgres createdb -O hfesmatch hfes
install -m 0644 /opt/hfes-match/deploy/hfes-match.service /etc/systemd/system/hfes-match.service
install -m 0644 /opt/hfes-match/deploy/hfes-match-worker.service /etc/systemd/system/hfes-match-worker.service
install -m 0644 /opt/hfes-match/deploy/nginx.conf /etc/nginx/sites-available/hfes-match
install -D -m 0644 /opt/hfes-match/deploy/security-headers.conf /etc/nginx/snippets/hfes-security-headers.conf
install -m 0644 /opt/hfes-match/deploy/logrotate-nginx /etc/logrotate.d/nginx   # access.log 1 day, error.log 7
# Watchdog: emails the owner when a unit is down, health fails, disk/memory are low, or a job is stuck/failed.
printf '*/5 * * * * root /usr/bin/python3 /opt/hfes-match/deploy/healthcheck.py >> /var/log/hfes-healthcheck.log 2>&1\n' > /etc/cron.d/hfes-healthcheck
chmod 0644 /etc/cron.d/hfes-healthcheck
rm -f /etc/nginx/sites-enabled/default
ln -sf /etc/nginx/sites-available/hfes-match /etc/nginx/sites-enabled/hfes-match
if [ ! -f /etc/hfes-match.env ]; then
  umask 077
  printf 'HFES_REEMBED_TOKEN=%s\n' "$(openssl rand -hex 32)" > /etc/hfes-match.env
fi
if ! grep -q '^HFES_AUTO_REBUILD_SECONDS=' /etc/hfes-match.env; then
  printf 'HFES_AUTO_REBUILD_SECONDS=180\nHF_HOME=/opt/hfes-match/state/model-cache\n' >> /etc/hfes-match.env
fi
chown -R hfesmatch:hfesmatch /opt/hfes-match
if [ ! -x /opt/hfes-match/.venv/bin/python ]; then
  # numba==0.67.0 (a umap-learn/pynndescent dependency) crashes deterministically
  # inside its own JIT bytecode analyzer on Python 3.14 (this box's system python3)
  # -- confirmed 2026-08-22, see CLAUDE.md. Use Python 3.12 via uv instead; numba
  # has long-established support there. Run uv as hfesmatch (whose $HOME is
  # /opt/hfes-match) so its managed interpreter lands under /opt/hfes-match, not
  # under this SSH user's home -- the service account must be able to read/execute
  # it, and a uv install under another user's home (e.g. ~ubuntu, mode 0750) isn't
  # traversable by hfesmatch.
  sudo -u hfesmatch bash -c 'curl -LsSf https://astral.sh/uv/install.sh | sh'
  sudo -u hfesmatch /opt/hfes-match/.local/bin/uv venv /opt/hfes-match/.venv --python 3.12
fi
# Runs on EVERY deploy, not just when the venv is new: a dependency added to
# requirements-frozen.txt (e.g. Flask/gunicorn) must reach an existing venv too, or the
# service can't start. Already-satisfied pins are a quick no-op.
  # Exact versions verified together on 2026-08-22. This is a CPU-only EC2 instance,
  # so torch needs PyTorch's CPU wheel index; --index-strategy unsafe-best-match
  # lets uv still pick the right versions of everything else off regular PyPI
  # instead of shadowing them with older releases that also happen to live on the
  # PyTorch index. /tmp on this box is RAM-backed (tmpfs) and too small to unpack
  # torch's wheel into, so extraction needs a real-disk TMPDIR.
  install -d -o hfesmatch -g hfesmatch /opt/hfes-match/.install-tmp
  sudo -u hfesmatch env TMPDIR=/opt/hfes-match/.install-tmp \
    /opt/hfes-match/.local/bin/uv pip install --python /opt/hfes-match/.venv/bin/python --no-cache \
    --index-url https://pypi.org/simple --extra-index-url https://download.pytorch.org/whl/cpu \
    --index-strategy unsafe-best-match \
    -r /opt/hfes-match/deploy/requirements-frozen.txt
  rm -rf /opt/hfes-match/.install-tmp
# Nginx runs as a different unprivileged user and must be able to read the
# public static data (including the UMAP JSON), while the SQLite state stays
# private to the service account.
# .local/.cache are excluded here too, not just state/.venv: they're uv's own install dirs
# (the real Python interpreter .venv/bin/python symlinks into, plus uv itself). A blanket
# chmod 0644 doesn't skip symlinks but DOES walk into the real directories reachable through
# them, silently stripping the +x bit off python3.12 and uv/uvx -- confirmed live on
# 2026-08-23: this is exactly what broke the service after a from-scratch venv install here,
# not the install itself.
find /opt/hfes-match \( -path /opt/hfes-match/state -o -path /opt/hfes-match/.venv -o -path /opt/hfes-match/.local -o -path /opt/hfes-match/.cache \) -prune -o -type d -exec chmod 0755 {} +
find /opt/hfes-match \( -path /opt/hfes-match/state -o -path /opt/hfes-match/.venv -o -path /opt/hfes-match/.local -o -path /opt/hfes-match/.cache \) -prune -o -type f -exec chmod 0644 {} +
chmod 0750 /opt/hfes-match/state
nginx -t
systemctl daemon-reload
systemctl enable hfes-match
systemctl restart hfes-match
# Warm embedding worker (see embed_umap.py run_worker). Restarting it mid-job is safe: server.py
# falls back to the cold subprocess for that job. It takes ~25 s to warm up after a restart.
systemctl enable hfes-match-worker
systemctl restart hfes-match-worker
systemctl enable --now nginx
systemctl reload nginx

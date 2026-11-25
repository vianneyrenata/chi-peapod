#!/usr/bin/env python3
"""Watchdog, run by cron every 5 minutes as root (installed by bootstrap-server.sh). Emails the owner
when something needs a human: a unit down, /api/health failing, the warm worker socket missing, low disk
or memory, a job stuck in the queue, or a job that failed recently. Deliberately independent of server.py
(no import, no Postgres connection needed to send) so it still works when the app itself is broken.
Each issue emails at most once per COOLDOWN; a "recovered" email is sent when it clears.
  python3 deploy/healthcheck.py --dry   # print instead of sending
"""
import json, os, shutil, smtplib, subprocess, sys, time, urllib.request
from email.message import EmailMessage
from pathlib import Path

ENV = Path("/etc/hfes-match.env"); STATE = Path("/opt/hfes-match/state/healthcheck"); COOLDOWN = 30 * 60
DRY = "--dry" in sys.argv

def env():
    out = {}
    for line in ENV.read_text().splitlines() if ENV.exists() else []:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1); out[k] = v.strip().strip('"').strip("'")
    return out

def sh(cmd):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60); return r.returncode, (r.stdout + r.stderr).strip()

def checks():
    found = []  # (key, title, detail, what_to_do)
    for unit in ("nginx", "hfes-match", "hfes-match-worker"):
        rc, out = sh(f"systemctl is-active {unit}")
        if out != "active":
            found.append((f"unit-{unit}", f"{unit} is {out or 'not running'}",
                          sh(f"journalctl -u {unit} -n 15 --no-pager")[1],
                          f"sudo systemctl restart {unit}   # then: sudo journalctl -u {unit} -n 50"))
    try:
        with urllib.request.urlopen("http://127.0.0.1/api/health", timeout=10) as r:
            body = json.load(r)
            if not body.get("ok"): raise RuntimeError(body)
    except Exception as exc:
        found.append(("api-health", "/api/health is not answering OK", str(exc),
                      "sudo systemctl status hfes-match nginx; sudo journalctl -u hfes-match -n 50"))
    if not Path("/opt/hfes-match/state/worker.sock").exists():
        found.append(("worker-sock", "warm worker socket missing (placements fall back to the slow path)",
                      sh("journalctl -u hfes-match-worker -n 15 --no-pager")[1], "sudo systemctl restart hfes-match-worker"))
    du = shutil.disk_usage("/")
    if du.free / du.total < 0.10:
        found.append(("disk", f"disk nearly full: {du.free/1e9:.1f} GB free of {du.total/1e9:.1f}", sh("du -sh /var/log /opt/hfes-match/state 2>/dev/null")[1],
                      "sudo journalctl --vacuum-size=50M; sudo apt-get clean; check /opt/hfes-match/state/model-cache"))
    mem = dict(l.split(":") for l in Path("/proc/meminfo").read_text().splitlines() if ":" in l)
    avail = int(mem.get("MemAvailable", "0 kB").split()[0]) // 1024
    if avail < 150:
        found.append(("memory", f"only {avail} MB memory available", sh("ps -eo rss,cmd --sort=-rss | head -5")[1],
                      "sudo systemctl restart hfes-match-worker   # it holds ~800 MB; restarting frees it"))
    rc, out = sh("sudo -u hfesmatch psql hfes -tAc \"SELECT status, count(*) FROM jobs WHERE (status='queued' AND requested_at < to_char(now() at time zone 'utc' - interval '10 minutes','YYYY-MM-DD\"T\"HH24:MI:SS')) OR (status='failed' AND finished_at > to_char(now() at time zone 'utc' - interval '10 minutes','YYYY-MM-DD\"T\"HH24:MI:SS')) GROUP BY 1\"")
    if rc == 0:
        for line in out.splitlines():
            st, n = line.split("|")
            if st == "queued":
                found.append(("jobs-stuck", f"{n} job(s) queued for more than 10 minutes", sh("curl -s http://127.0.0.1/api/rebuild")[1],
                              "A restart re-arms queued jobs: sudo systemctl restart hfes-match"))
            if st == "failed":
                found.append(("jobs-failed", f"{n} job(s) failed in the last 10 minutes (see the job's own email for the fix)", sh("curl -s http://127.0.0.1/api/rebuild")[1],
                              "Follow the 'Placement failed' email; this is just a reminder."))
    elif "psql" in out or rc:
        found.append(("postgres", "cannot query Postgres", out[-500:], "sudo systemctl status postgresql; sudo systemctl restart postgresql"))
    return found

def send(subject, body):
    e = env()
    if DRY or not (e.get("HFES_ALERT_EMAIL_TO") and e.get("HFES_ALERT_SMTP_USER") and e.get("HFES_ALERT_SMTP_PASSWORD")):
        print(f"--- {subject}\n{body}\n"); return
    msg = EmailMessage(); msg["Subject"] = subject; msg["From"] = e["HFES_ALERT_SMTP_USER"]; msg["To"] = e["HFES_ALERT_EMAIL_TO"]; msg.set_content(body)
    with smtplib.SMTP(e.get("HFES_ALERT_SMTP_HOST", "smtp.gmail.com"), int(e.get("HFES_ALERT_SMTP_PORT", "587")), timeout=20) as smtp:
        smtp.starttls(); smtp.login(e["HFES_ALERT_SMTP_USER"], e["HFES_ALERT_SMTP_PASSWORD"]); smtp.send_message(msg)

def main():
    STATE.mkdir(parents=True, exist_ok=True)
    now = time.time(); found = checks(); keys = {k for k, *_ in found}
    for key, title, detail, todo in found:
        stamp = STATE / key
        if stamp.exists() and now - stamp.stat().st_mtime < COOLDOWN:
            continue
        send(f"[CHI Peapod] ACTION: {title}", f"{title}\n\nWHAT TO DO (on the server, ssh to the box):\n  {todo}\n\nDetail:\n{detail}\n\n"
             f"Sent by deploy/healthcheck.py (cron, every 5 min; this issue is re-sent at most every {COOLDOWN//60} min while it persists).")
        stamp.touch()
    for stamp in STATE.iterdir():
        if stamp.name not in keys and stamp.name != "recovered":
            send(f"[CHI Peapod] recovered: {stamp.name}", f"The '{stamp.name}' issue is no longer detected. Nothing to do.")
            stamp.unlink()
    if DRY: print(f"checks run; issues: {[k for k, *_ in found] or 'none'}")

if __name__ == "__main__":
    main()

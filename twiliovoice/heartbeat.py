#!/usr/bin/env python3
"""Heartbeat monitor for a scheduled or webhook job (Vibe Coding Playbook rule H13).

Reusable template for client builds. Stdlib only, carries NO secrets: every
address and password comes from environment variables set in the CLIENT's own
secret store. Modelled on Neo's shared/job_alert.py (never raises, one alert per
job per cooldown window), with three additions the playbook asks for:

1. FAILURE alert: the job raised an error or exited non-zero.
2. EMPTY alert: the job "succeeded" but produced nothing (0 records, no marker).
   A silent empty run is how an n8n lead workflow lost 40 leads in 3 days.
3. MISSED-HEARTBEAT alert: the job stopped running at all. Either a hosted
   dead-man's switch (Healthchecks.io style ping URL: it alerts when pings stop)
   or the local `check` command run on a second schedule.

Who is alerted: the CLIENT's named person first (ALERT_EMAIL_TO / ALERT_WEBHOOK_URL
point at them); add our address as a backup only under a care plan.

Environment variables (all optional; with none set, alerts print to stderr):
  HEARTBEAT_PING_URL   e.g. https://hc-ping.com/<uuid> (we call /start, /fail, or the bare URL)
  ALERT_WEBHOOK_URL    POSTs JSON {"job","kind","reason","detail"} (Slack/Teams/n8n/Make webhook)
  ALERT_EMAIL_TO       comma-separated; needs ALERT_SMTP_HOST (+ _PORT, _USER, _PASSWORD, ALERT_EMAIL_FROM)
  HEARTBEAT_STATE_DIR  where last-success and cooldown files live (default ~/.heartbeat)

Use in Python:
    from heartbeat import Heartbeat
    hb = Heartbeat("Lead sync", min_items=1)
    hb.run(sync_leads)          # sync_leads returns a list (or a count) of records handled

Use around any command (cron / launchd / Task Scheduler):
    python3 heartbeat.py wrap --job "Lead sync" --min-items 1 -- python3 sync.py
    # the command prints a line HEARTBEAT_ITEMS=<n>; missing marker counts as empty
    python3 heartbeat.py check --job "Lead sync" --max-age-hours 26   # on its own schedule
"""
import argparse
import json
import os
import smtplib
import subprocess
import sys
import time
import traceback
import urllib.request
from email.message import EmailMessage
from pathlib import Path

DEFAULT_COOLDOWN = 20 * 3600
MARKER = "HEARTBEAT_ITEMS="


def _slug(job: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in job.lower()).strip("-") or "job"


class Heartbeat:
    def __init__(self, job, ping_url=None, alert=None, state_dir=None,
                 cooldown=DEFAULT_COOLDOWN, min_items=1, timeout=10):
        self.job = job
        self.ping_url = (ping_url if ping_url is not None else os.environ.get("HEARTBEAT_PING_URL", "")).rstrip("/")
        self.alert_fn = alert or default_alert
        self.state_dir = Path(state_dir or os.environ.get("HEARTBEAT_STATE_DIR") or Path.home() / ".heartbeat")
        self.cooldown = cooldown
        self.min_items = min_items
        self.timeout = timeout

    # ---- state -------------------------------------------------------------
    def _state_file(self):
        return self.state_dir / f"{_slug(self.job)}.json"

    def _load(self):
        try:
            return json.loads(self._state_file().read_text())
        except Exception:
            return {}

    def _save(self, state):
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            self._state_file().write_text(json.dumps(state))
        except Exception as e:
            print(f"[heartbeat] could not save state: {e}", file=sys.stderr)

    def last_success(self):
        return self._load().get("last_success")

    # ---- outbound ----------------------------------------------------------
    def ping(self, suffix="", body=""):
        """Ping the dead-man's switch. Never raises: a monitor outage must not break the job."""
        if not self.ping_url:
            return False
        url = self.ping_url + (f"/{suffix}" if suffix else "")
        try:
            req = urllib.request.Request(url, data=body.encode()[:100_000] or None, method="POST")
            with urllib.request.urlopen(req, timeout=self.timeout):
                return True
        except Exception as e:
            print(f"[heartbeat] ping {suffix or 'success'} failed: {e}", file=sys.stderr)
            return False

    def alert(self, kind, reason, detail=""):
        """Send one alert per job+kind per cooldown window. Returns True if sent."""
        state = self._load()
        key = f"alerted_{kind}"
        now = time.time()
        if now - state.get(key, 0) < self.cooldown:
            print(f"[heartbeat] {self.job}: {kind} already alerted within cooldown", file=sys.stderr)
            return False
        try:
            sent = bool(self.alert_fn(self.job, kind, reason, (detail or "")[:2000]))
        except Exception as e:
            print(f"[heartbeat] alert error: {e}", file=sys.stderr)
            sent = False
        if sent:
            state = self._load()
            state[key] = now
            self._save(state)
        return sent

    # ---- the three checks ----------------------------------------------------
    def record(self, items):
        """Call at the end of a run with the number of records handled."""
        if items < self.min_items:
            self.ping("fail", f"empty run: {items} item(s), expected at least {self.min_items}")
            self.alert("empty", f"ran but handled {items} item(s); expected at least {self.min_items}")
            return False
        state = self._load()
        state.update(last_success=time.time(), last_items=items)
        state.pop("alerted_empty", None)
        state.pop("alerted_failure", None)
        state.pop("alerted_missed", None)
        self._save(state)
        self.ping("", f"ok: {items} item(s)")
        return True

    def run(self, fn, *args, **kwargs):
        """Run fn with start/success/fail pings and failure + empty alerts. Re-raises errors."""
        self.ping("start")
        try:
            result = fn(*args, **kwargs)
        except Exception as e:
            tb = traceback.format_exc()
            self.ping("fail", tb)
            self.alert("failure", f"{type(e).__name__}: {e}", tb)
            raise
        items = result if isinstance(result, int) and not isinstance(result, bool) else (
            len(result) if hasattr(result, "__len__") else (0 if result is None else 1))
        self.record(items)
        return result

    def check_missed(self, max_age_seconds):
        """For setups with no hosted ping service: run this on its OWN schedule."""
        last = self.last_success()
        if last is None:
            return self.alert("missed", "no successful run has ever been recorded") or True
        age = time.time() - last
        if age > max_age_seconds:
            self.alert("missed", f"last successful run was {age / 3600:.1f} h ago "
                                 f"(limit {max_age_seconds / 3600:.1f} h)")
            return True
        return False


def default_alert(job, kind, reason, detail=""):
    """Webhook and/or email from env vars; stderr if neither is set. True if any channel worked."""
    text = f"[{kind.upper()}] {job}: {reason}"
    sent = False
    hook = os.environ.get("ALERT_WEBHOOK_URL")
    if hook:
        try:
            payload = json.dumps({"job": job, "kind": kind, "reason": reason, "detail": detail, "text": text})
            req = urllib.request.Request(hook, data=payload.encode(), method="POST",
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10):
                sent = True
        except Exception as e:
            print(f"[heartbeat] webhook alert failed: {e}", file=sys.stderr)
    to = os.environ.get("ALERT_EMAIL_TO")
    host = os.environ.get("ALERT_SMTP_HOST")
    if to and host:
        try:
            msg = EmailMessage()
            msg["Subject"] = f"Automation alert: {job} ({kind})"
            msg["From"] = os.environ.get("ALERT_EMAIL_FROM", os.environ.get("ALERT_SMTP_USER", "alerts@localhost"))
            msg["To"] = to
            msg.set_content(f"{text}\n\nDetail:\n{detail or '(none)'}\n")
            with smtplib.SMTP(host, int(os.environ.get("ALERT_SMTP_PORT", "587")), timeout=20) as s:
                if os.environ.get("ALERT_SMTP_USER"):
                    s.starttls()
                    s.login(os.environ["ALERT_SMTP_USER"], os.environ.get("ALERT_SMTP_PASSWORD", ""))
                s.send_message(msg)
            sent = True
        except Exception as e:
            print(f"[heartbeat] email alert failed: {e}", file=sys.stderr)
    if not sent:
        print(f"[heartbeat] ALERT (no channel configured or all failed): {text}", file=sys.stderr)
    return sent


def _cmd_wrap(a):
    hb = Heartbeat(a.job, min_items=a.min_items)
    hb.ping("start")
    try:
        proc = subprocess.run(a.command, capture_output=True, text=True, timeout=a.timeout)
    except Exception as e:
        hb.ping("fail", str(e))
        hb.alert("failure", f"could not run command: {e}")
        return 1
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    if proc.returncode != 0:
        hb.ping("fail", (proc.stderr or proc.stdout)[-5000:])
        hb.alert("failure", f"exit code {proc.returncode}", (proc.stderr or proc.stdout)[-2000:])
        return proc.returncode
    marks = [ln for ln in proc.stdout.splitlines() if ln.startswith(MARKER)]
    try:
        items = int(marks[-1][len(MARKER):]) if marks else 0
    except ValueError:
        items = 0
    if not marks:
        hb.ping("fail", "no HEARTBEAT_ITEMS marker in output")
        hb.alert("empty", "command finished but printed no HEARTBEAT_ITEMS=<n> line", proc.stdout[-2000:])
        return 3
    return 0 if hb.record(items) else 2


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("wrap", help="run a command with heartbeat, failure and empty-output alerts")
    w.add_argument("--job", required=True)
    w.add_argument("--min-items", type=int, default=1)
    w.add_argument("--timeout", type=int, default=3600)
    w.add_argument("command", nargs=argparse.REMAINDER)
    c = sub.add_parser("check", help="alert if the last successful run is too old")
    c.add_argument("--job", required=True)
    c.add_argument("--max-age-hours", type=float, required=True)
    a = p.parse_args(argv)
    if a.cmd == "wrap":
        if a.command and a.command[0] == "--":
            a.command = a.command[1:]
        if not a.command:
            p.error("wrap needs a command after --")
        return _cmd_wrap(a)
    return 1 if Heartbeat(a.job).check_missed(a.max_age_hours * 3600) else 0


if __name__ == "__main__":
    sys.exit(main())

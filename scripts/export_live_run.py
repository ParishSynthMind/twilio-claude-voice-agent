"""Export a live run as evidence for examples/live-run/, with phone numbers masked.

    python scripts/export_live_run.py --data var/live --out examples/live-run [--twilio]

Writes calls.json (transcripts, tool calls, outcomes, per-turn latency), outbox-or-emails.json
(the team emails' subjects and bodies, masked), and with --twilio, twilio-calls.json: Twilio's own
record of each call (status, direction, duration, price) read from the REST API. Nothing secret is
written: no auth token, no account SID, no full phone number.
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PHONE = re.compile(r"\+\d{7,15}")
ACCOUNT = re.compile(r"AC[0-9a-f]{32}")


def mask(text):
    if not isinstance(text, str):
        return text
    text = ACCOUNT.sub("AC<masked>", text)
    return PHONE.sub(lambda m: m.group()[:5] + "*" * (len(m.group()) - 8) + m.group()[-3:], text)


def deep(obj):
    if isinstance(obj, dict):
        return {k: deep(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [deep(v) for v in obj]
    return mask(obj)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="var/live")
    ap.add_argument("--out", default="examples/live-run")
    ap.add_argument("--twilio", action="store_true", help="also read Twilio's call records (Keychain keys)")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    from twiliovoice.calllog import CallLog

    log = CallLog(Path(a.data) / "calls.sqlite3")
    calls = log.calls(100)
    (out / "calls.json").write_text(json.dumps(deep(calls), indent=2, default=str))
    emails = []
    outbox = Path(a.data) / "outbox.jsonl"
    if outbox.exists():
        emails = [json.loads(line) for line in outbox.read_text().splitlines() if line.strip()]
    (out / "team-emails.json").write_text(json.dumps(deep(emails), indent=2))
    if a.twilio:
        from twiliovoice import secrets_store
        from twiliovoice.twilio_api import TwilioVoiceClient

        sid = secrets_store.get("TWILIO_ACCOUNT_SID", "neo-twilio-trial-sid")
        tok = secrets_store.get("TWILIO_AUTH_TOKEN", "neo-twilio-trial-token")
        tw = TwilioVoiceClient(sid, tok)
        keep = ("sid", "direction", "status", "start_time", "end_time", "duration", "price", "price_unit",
                "answered_by", "from", "to")
        records = []
        for c in calls:
            try:
                info = tw.get_call(c["call_sid"])
            except Exception as exc:  # a simulated call has no Twilio record
                records.append({"sid": c["call_sid"], "error": type(exc).__name__})
                continue
            records.append({k: info.get(k) for k in keep})
        (out / "twilio-calls.json").write_text(json.dumps(deep(records), indent=2))
    print(f"exported {len(calls)} calls, {len(emails)} emails -> {out}")


if __name__ == "__main__":
    main()

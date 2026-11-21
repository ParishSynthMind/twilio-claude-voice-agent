"""Run one whole phone call against the app with the simulated Twilio (no account, no phone).

    python scripts/simulate_call.py                       # offline, FakeBrain
    ANTHROPIC_API_KEY=... python scripts/simulate_call.py --live-claude --out examples/simulated-claude

Writes transcript.json (what the caller heard and said, the TwiML, tool calls) and outbox.jsonl
(the emails the team would get)."""
import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from twiliovoice.app import build_agent, create_app  # noqa: E402
from twiliovoice.brain import FakeBrain  # noqa: E402
from twiliovoice.config import Settings  # noqa: E402
from twiliovoice.mock_twilio import FakeTwilioREST, MockTwilio  # noqa: E402

LINES = ["Hi, do you have parking?", "Great. Can I book a check-up tomorrow morning?",
         "The earliest one please. My name is Sam Patel.", "No, that's everything. Bye."]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live-claude", action="store_true")
    ap.add_argument("--out", default="")
    ap.add_argument("--line", action="append", help="caller lines (repeat); default is a booking call")
    a = ap.parse_args()
    data = Path(a.out) if a.out else Path(tempfile.mkdtemp())
    data.mkdir(parents=True, exist_ok=True)
    s = Settings(data_dir=data, public_base_url="https://voice.example.test", twilio_account_sid="AC" + "0" * 32,
                 twilio_auth_token="simulated-token", twilio_from_number="+447700900000", reply_wait_seconds=12)
    agent = build_agent(s, brain=None if a.live_claude else FakeBrain(), twilio=FakeTwilioREST())
    with TestClient(create_app(s, agent=agent)) as client:
        tw = MockTwilio(client, "simulated-token", s.public_base_url, account_sid=s.twilio_account_sid)
        call = tw.run(s.public_base_url + "/twilio/voice/inbound", a.line or LINES)
    out = {"call_sid": call.sid, "brain": "claude " + s.model if a.live_claude else "fake",
           "conversation": agent.log.transcript(call.sid), "tool_events": agent.log.tool_events(call.sid),
           "call": agent.log.get(call.sid), "twiml": call.twiml}
    (data / "transcript.json").write_text(json.dumps(out, indent=2, default=str))
    for t in out["conversation"]:
        print(f"{t['speaker']:>6}: {t['text']}")
    print("tools:", [e["tool"] for e in out["tool_events"]], "->", data)


if __name__ == "__main__":
    main()

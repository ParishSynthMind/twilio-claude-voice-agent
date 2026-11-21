"""A simulated Twilio for tests, CI and the recorded demo: no account, no phone, no network.

It behaves the way the live run showed real Twilio behaves (see docs/TWILIO-FACTS.md):
it POSTs form-encoded webhooks signed with X-Twilio-Signature over the PUBLIC URL, reads the TwiML
answer, "speaks" every <Say>, answers each <Gather input="speech"> with the next scripted caller
line (an empty string = silence, sent because actionOnEmptyResult is true), follows <Redirect>,
simulates <Dial> with a DialCallStatus, stops at <Hangup>, then sends the final status callback
with the call's duration.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional
from urllib.parse import urlencode, urlsplit

from . import twilio_signature
from .twiml import parse


@dataclass
class SimCall:
    sid: str
    heard: List[str] = field(default_factory=list)       # everything the caller heard, in order
    said: List[str] = field(default_factory=list)        # the caller's lines that were sent
    twiml: List[str] = field(default_factory=list)       # every TwiML document returned
    requests: List[Dict[str, str]] = field(default_factory=list)
    dialled: List[str] = field(default_factory=list)
    hung_up: bool = False
    duration: int = 0


class MockTwilio:
    def __init__(self, client, auth_token: str, public_base_url: str, account_sid: str = "AC" + "0" * 32,
                 trial_number: str = "+447700900000", caller: str = "+447700900123",
                 dial_status: str = "completed", seconds_per_turn: int = 8):
        self.client = client                 # a FastAPI TestClient (or anything with .post(path, ...))
        self.auth_token = auth_token
        self.base = public_base_url.rstrip("/")
        self.account_sid = account_sid
        self.trial_number = trial_number
        self.caller = caller
        self.dial_status = dial_status
        self.seconds_per_turn = seconds_per_turn

    def _post(self, url: str, params: Dict[str, str], sign: bool = True):
        parts = urlsplit(url)
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        items = list(params.items())
        headers = twilio_signature.sign(self.auth_token, url, items) if sign else \
            {"Content-Type": "application/x-www-form-urlencoded"}
        return self.client.post(path, content=urlencode(items), headers=headers)

    def _base_params(self, call: SimCall, direction: str, frm: str, to: str) -> Dict[str, str]:
        return {"AccountSid": self.account_sid, "CallSid": call.sid, "From": frm, "To": to,
                "CallStatus": "in-progress", "Direction": direction, "ApiVersion": "2010-04-01"}

    def run(self, first_url: str, lines: List[str], direction: str = "inbound", frm: Optional[str] = None,
            to: Optional[str] = None, sid: Optional[str] = None, max_steps: int = 60,
            on_step: Optional[Callable[[SimCall], None]] = None, answered_by: str = "") -> SimCall:
        """Drive one call from its first webhook to hang-up, then send the status callback."""
        call = SimCall(sid=sid or "CA" + uuid.uuid4().hex)
        frm = frm or (self.caller if direction == "inbound" else self.trial_number)
        to = to or (self.trial_number if direction == "inbound" else self.caller)
        base = self._base_params(call, direction if direction == "inbound" else "outbound-api", frm, to)
        if answered_by:
            base["AnsweredBy"] = answered_by
        queue = list(lines)
        url, params = first_url, dict(base)
        for _ in range(max_steps):
            call.requests.append({"url": url, **params})
            r = self._post(url, params)
            if r.status_code != 200:
                raise AssertionError(f"{url} answered HTTP {r.status_code}: {r.text[:200]}")
            call.twiml.append(r.text)
            nxt = self._play(call, r.text, base, queue)
            if on_step:
                on_step(call)
            if nxt is None:
                break
            url, params = nxt
        self.send_status(call, frm, to, "completed", call.duration)
        return call

    def _play(self, call: SimCall, xml: str, base: Dict[str, str], queue: List[str]):
        root = parse(xml)
        for el in root:
            if el.tag == "Say":
                call.heard.append((el.text or "").strip())
            elif el.tag == "Pause":
                pass
            elif el.tag == "Gather":
                for child in el:
                    if child.tag == "Say":
                        call.heard.append((child.text or "").strip())
                line = queue.pop(0) if queue else None
                if line is None:
                    call.hung_up = True   # the scripted caller hangs up when out of lines
                    return None
                call.said.append(line)
                call.duration += self.seconds_per_turn
                p = dict(base)
                if line:
                    p.update(SpeechResult=line, Confidence="0.91")
                return el.get("action"), p
            elif el.tag == "Redirect":
                return (el.text or "").strip(), dict(base)
            elif el.tag == "Dial":
                number = next((c.text for c in el if c.tag == "Number"), el.text or "")
                call.dialled.append(number)
                call.duration += 30
                p = dict(base, DialCallStatus=self.dial_status, DialCallDuration="30")
                return el.get("action"), p
            elif el.tag == "Hangup":
                call.hung_up = True
                return None
        call.hung_up = True   # TwiML ran out of verbs: Twilio ends the call
        return None

    def send_status(self, call: SimCall, frm: str, to: str, status: str, duration: int, sign: bool = True):
        p = {"AccountSid": self.account_sid, "CallSid": call.sid, "From": frm, "To": to,
             "CallStatus": status, "CallDuration": str(duration), "Duration": str((duration + 59) // 60),
             "Direction": "inbound", "ApiVersion": "2010-04-01"}
        return self._post(f"{self.base}/twilio/voice/status", p, sign=sign)


class FakeTwilioREST:
    """Stands in for TwilioVoiceClient in tests: records calls instead of dialling."""

    def __init__(self, fail_with=None):
        self.created: List[dict] = []
        self.hung_up: List[str] = []
        self.fail_with = fail_with
        self.calls: Dict[str, dict] = {}   # what get_call answers, set by tests

    def create_call(self, **kw):
        if self.fail_with:
            raise self.fail_with
        from .twilio_api import CallResult

        sid = "CA" + uuid.uuid4().hex
        self.created.append({"sid": sid, **kw})
        return CallResult(sid=sid, status="queued", raw={"sid": sid, "status": "queued"})

    def get_call(self, call_sid):
        return self.calls.get(call_sid, {"sid": call_sid, "status": "in-progress"})

    def hangup(self, call_sid):
        self.hung_up.append(call_sid)
        return {"sid": call_sid, "status": "completed"}

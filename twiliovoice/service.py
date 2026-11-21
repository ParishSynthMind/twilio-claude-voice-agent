"""The phone agent: what happens on each Twilio webhook, as TwiML.

Call flow (inbound and outbound are the same after the first line):
1. The call connects -> the AI disclosure and the recording notice are spoken FIRST, inside a
   <Gather input="speech"> so the caller can answer straight away.
2. Twilio posts what the caller said (SpeechResult) -> Claude answers, using tools when needed ->
   the answer is spoken inside the next <Gather>. Silence twice -> a polite goodbye.
3. Hand-over -> the team gets an email (URGENT if flagged); if a person's number is set (and the
   account is not a trial, where <Dial> is blocked) the call is transferred with the AI's
   hand-over notice, else the caller is promised a call back.
4. Caps: every turn checks the call's age and turn count; the cost guard is checked before the AI
   takes a call; the kill switch stops everything.
5. The call ends -> Twilio's status callback gives the duration -> the team is emailed a summary
   once, the minutes count towards the daily budget, and the heartbeat is pinged.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from .brain import FALLBACK, TurnResult
from .calllog import CallLog
from .config import Settings
from .costguard import CostGuard, OutboundRefused, check_outbound
from .notify import Notifier
from .tools import VoiceAgentTools, _clean
from .twilio_api import FINAL, TwilioVoiceClient
from .twiml import GatherSpec, Response, Voice

log = logging.getLogger("twiliovoice")

PATHS = {
    "inbound": "/twilio/voice/inbound",
    "outbound": "/twilio/voice/outbound",
    "gather": "/twilio/voice/gather",
    "pending": "/twilio/voice/pending",
    "status": "/twilio/voice/status",
    "dial": "/twilio/voice/dial-status",
}
NO_INPUT = "Sorry, I didn't catch that. Could you say it again?"
GOODBYE_SILENCE = "I haven't heard anything, so I'll end the call now. Please call back any time. Goodbye."
WRAP_UP = ("I'm sorry, I need to wrap up this call now. If there's anything else, the team will be happy "
           "to call you back. Goodbye.")
GUARD_LINE = ("I can't help on this line right now, but I've let the team know and someone will call you back "
              "as soon as possible. Goodbye.")
ONE_MOMENT = "One moment."


class PhoneAgent:
    def __init__(self, settings: Settings, brain, tools: VoiceAgentTools, log_: CallLog, guard: CostGuard,
                 notifier: Notifier, twilio: Optional[TwilioVoiceClient] = None,
                 alert: Optional[Callable[[str, str], None]] = None, heartbeat=None):
        self.s = settings
        self.brain = brain
        self.tools = tools
        self.log = log_
        self.guard = guard
        self.notifier = notifier
        self.twilio = twilio
        self.alert = alert
        self.heartbeat = heartbeat
        self.voice = Voice(settings.tts_voice, settings.language)

    # helpers -------------------------------------------------------------------------------------
    def url(self, name: str) -> str:
        return f"{self.s.public_base_url}{PATHS[name]}"

    def _response(self) -> Response:
        return Response(self.voice)

    def _gather(self, text: str) -> str:
        spec = GatherSpec(action=self.url("gather"), language=self.s.language,
                          speech_timeout=self.s.speech_timeout, speech_model=self.s.speech_model,
                          hints=["appointment", "parking", "check-up", "hygienist", "whitening", "cancel"])
        return self._response().gather(text, spec).xml()

    def _say_hangup(self, text: str) -> str:
        return self._response().say(text).hangup().xml()

    def _call(self, p: Dict[str, str]) -> Dict[str, Any]:
        sid = p.get("CallSid", "")
        row = self.log.get(sid) or {}
        caller = row.get("callee") if row.get("direction") == "outbound" else row.get("caller")
        return {"id": sid, "caller": caller or p.get("From", "")}

    def _guard_refusal(self, p: Dict[str, str], reason: str) -> str:
        sid = p.get("CallSid", "")
        caller = p.get("From", "")
        self.notifier.send("Callback needed (phone agent not taking calls)",
                           f"A call came in while the phone agent was stopped ({reason}).\n\n"
                           f"Caller: {caller}\nCall sid: {sid}\nPlease call them back.\n")
        self.log.add_outcome(sid, f"Not answered by AI: {reason}")
        return self._say_hangup(f"{self.s.disclosure} {GUARD_LINE}")

    # webhooks ------------------------------------------------------------------------------------
    def inbound(self, p: Dict[str, str]) -> str:
        sid = p.get("CallSid", "")
        st = self.guard.check(starting_new_call=True)
        self.log.start(sid, "inbound", p.get("From", ""), p.get("To", ""), self.guard.today())
        if not st.allowed:
            return self._guard_refusal(p, st.reason)
        greeting = self.s.first_message
        if not self.log.transcript(sid):
            self.log.add_turn(sid, "agent", greeting)
        return self._gather(greeting)

    def outbound_answered(self, p: Dict[str, str]) -> str:
        sid = p.get("CallSid", "")
        row = self.log.get(sid) or {}
        if self.guard.paused:
            return self._say_hangup(f"{self.s.outbound_disclosure} Sorry, we'll call you another time. Goodbye.")
        if (p.get("AnsweredBy") or "").startswith("machine"):
            self.log.add_outcome(sid, "Voicemail: hung up without leaving a message")
            return self._response().hangup().xml()
        greeting = self.s.outbound_first_message(row.get("purpose") or "your appointment")
        if not self.log.transcript(sid):
            self.log.add_turn(sid, "agent", greeting)
        return self._gather(greeting)

    def over_caps(self, sid: str) -> Optional[str]:
        row = self.log.get(sid) or {}
        if self.guard.paused:
            return "paused (kill switch)"
        if (row.get("turns") or 0) >= self.s.max_turns:
            return f"turn limit ({self.s.max_turns})"
        started = row.get("started_utc")
        if started:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(started)).total_seconds()
            if age >= self.s.max_call_seconds - 15:
                return f"call length limit ({self.s.max_call_seconds} s)"
        return None

    def empty_speech(self, p: Dict[str, str]) -> str:
        sid = p.get("CallSid", "")
        if self.log.bump(sid, "empty_turns") >= 2:
            self.log.add_turn(sid, "agent", GOODBYE_SILENCE)
            self.log.add_outcome(sid, "Ended: no speech heard")
            self.log.mark(sid, ended=1)
            return self._say_hangup(GOODBYE_SILENCE)
        self.log.add_turn(sid, "agent", NO_INPUT)
        return self._gather(NO_INPUT)

    def record_speech(self, p: Dict[str, str]) -> Optional[str]:
        """Store the caller's words. Returns TwiML to send at once (caps hit), else None."""
        sid = p.get("CallSid", "")
        if not self.log.get(sid):  # e.g. the server restarted mid-call: keep going, don't crash
            self.log.start(sid, (p.get("Direction") or "inbound").split("-")[0], p.get("From", ""),
                           p.get("To", ""), self.guard.today())
        text = _clean(p.get("SpeechResult"), 500)
        conf = p.get("Confidence")
        self.log.add_turn(sid, "caller", text, float(conf) if conf not in (None, "") else None)
        self.log.reset_empty(sid)
        self.log.bump(sid, "turns")
        hit = self.over_caps(sid)
        if hit:
            self.log.add_turn(sid, "agent", WRAP_UP)
            self.log.add_outcome(sid, f"Ended: {hit}")
            self.log.mark(sid, ended=1)
            return self._say_hangup(WRAP_UP)
        return None

    def think(self, p: Dict[str, str]) -> TurnResult:
        """The slow part (Claude + tools). Runs in a worker thread; never raises."""
        sid = p.get("CallSid", "")
        call = self._call(p)
        started = time.monotonic()
        try:
            result = self.brain.turn(self.log.transcript(sid), lambda name, args: self.tools.run(name, args, call),
                                     self.guard.now().strftime("%A %d %B %Y"))
            result.latency_ms = int((time.monotonic() - started) * 1000)
            log.info("turn %s: %s ms, tools %s, tokens %s/%s", sid, result.latency_ms, result.tools_used,
                     result.input_tokens, result.output_tokens)
            return result
        except Exception as exc:  # belt and braces: the brain already falls back on its own errors
            log.exception("brain failed")
            if self.alert:
                self.alert("Phone agent brain failed", f"{type(exc).__name__}: {exc} (call {sid})")
            return TurnResult(say=FALLBACK, source="fallback")

    def answer(self, p: Dict[str, str], result: TurnResult) -> str:
        sid = p.get("CallSid", "")
        self.log.add_turn(sid, "agent", result.say, latency_ms=result.latency_ms or None)
        if result.source == "fallback" and self.alert:
            self.alert("Phone agent fell back to the fixed line", f"{result.tools_used} (call {sid})")
        if result.end_call:
            self.log.mark(sid, ended=1)
            return self._say_hangup(result.say)
        if result.handover and self.s.human_number and not self.s.trial_mode:
            notice = self.s.handover_notice
            text = " ".join(x for x in (result.say if notice in result.say else f"{notice} {result.say}",
                                        "Connecting you now.") if x)
            self.log.add_outcome(sid, f"Transferred to {self.s.human_number}")
            return (self._response().say(text)
                    .dial(self.s.human_number, self.url("dial"), time_limit=self.s.max_call_seconds * 2)
                    .say("Sorry, nobody could take the call. The team will call you back. Goodbye.").hangup().xml())
        return self._gather(result.say)

    def still_thinking(self, p: Dict[str, str], round_: int) -> str:
        """Claude needed longer than REPLY_WAIT_SECONDS: keep the line alive, then come back."""
        if round_ >= self.s.max_pending_rounds:
            return self.answer(p, TurnResult(say=FALLBACK, source="fallback", tools_used=["(fallback: too slow)"]))
        r = self._response()
        if round_ == 0:
            r.say(ONE_MOMENT)
        return r.pause(1).redirect(f"{self.url('pending')}?round={round_ + 1}").xml()

    def dial_status(self, p: Dict[str, str]) -> str:
        sid = p.get("CallSid", "")
        st = p.get("DialCallStatus", "")
        self.log.add_outcome(sid, f"Transfer {st or 'ended'}")
        if st == "completed":
            return self._response().hangup().xml()
        return self._say_hangup("Sorry, nobody could take the call. The team will call you back. Goodbye.")

    def status(self, p: Dict[str, str]) -> None:
        sid, st = p.get("CallSid", ""), (p.get("CallStatus") or "").lower()
        if not sid or not st or not self.log.first_status(sid, st):
            return
        try:
            fields: Dict[str, Any] = {"status": st}
            if p.get("CallDuration", "").isdigit():
                fields["duration_seconds"] = int(p["CallDuration"])
            if not self.log.get(sid):
                self.log.start(sid, (p.get("Direction") or "inbound").split("-")[0], p.get("From", ""),
                               p.get("To", ""), self.guard.today())
            self.log.mark(sid, **fields)
            if st in FINAL:
                self.log.mark(sid, ended=1)
                self.guard.check()
                self.send_summary(sid)
        except Exception:
            self.log.forget_status(sid, st)
            raise

    def send_summary(self, sid: str) -> bool:
        row = self.log.get(sid) or {}
        turns = self.log.transcript(sid)
        caller_turns = [t for t in turns if t["speaker"] == "caller"]
        if not self.log.claim_summary(sid):
            return False
        who = row.get("callee") if row.get("direction") == "outbound" else row.get("caller")
        lines = "\n".join(f"{'Caller' if t['speaker'] == 'caller' else 'Agent'}: {t['text']}" for t in turns)
        body = (f"{(row.get('direction') or 'inbound').capitalize()} call, {row.get('status') or 'ended'}, "
                f"{row.get('duration_seconds') or 0} s\nNumber: {who or 'unknown'}\n"
                f"Outcome: {row.get('outcome') or 'Questions answered'}\n"
                f"Handed over: {'yes' if row.get('handover') else 'no'}\nCall sid: {sid}\n\nTranscript\n{lines}\n")
        self.notifier.send(f"Call summary: {who or 'unknown caller'}", body)
        if self.heartbeat and caller_turns:
            self.heartbeat.record(1)
        return True

    # outbound ------------------------------------------------------------------------------------
    def place_call(self, to: str, purpose: str, now_utc: Optional[datetime] = None) -> Dict[str, Any]:
        """Ring someone. Refused (OutboundRefused) unless the guard, the country list and the
        recipient's calling hours all allow it."""
        if self.twilio is None:
            raise OutboundRefused("Twilio is not configured (TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN).")
        if not self.s.twilio_from_number:
            raise OutboundRefused("TWILIO_FROM_NUMBER is not set.")
        st = self.guard.check(starting_new_call=True)
        if not st.allowed:
            raise OutboundRefused(f"Cost guard: {st.reason}.")
        check_outbound(to, self.s.outbound_allowed_prefixes, self.s.calling_windows,
                       now_utc or datetime.now(timezone.utc))
        purpose = _clean(purpose, 120) or "your appointment"
        res = self.twilio.create_call(to=to, from_number=self.s.twilio_from_number, url=self.url("outbound"),
                                      status_callback=self.url("status"), time_limit=self.s.max_call_seconds,
                                      record=self.s.record_calls, machine_detection=True)
        self.log.start(res.sid, "outbound", self.s.twilio_from_number, to, self.guard.today(), purpose)
        self.log.mark(res.sid, status=res.status)
        return {"call_sid": res.sid, "status": res.status}

    def reconcile(self, older_than_seconds: int = 120) -> List[Dict[str, Any]]:
        """Missed-webhook safety net. Calls that never got a final status callback (Twilio trial
        numbers take no status-callback URL for inbound calls, and a callback can be lost) are
        looked up through the REST API and finished: duration recorded, summary emailed once."""
        done: List[Dict[str, Any]] = []
        if self.twilio is None:
            return done
        now = datetime.now(timezone.utc)
        for c in self.log.calls(200):
            if (c.get("status") or "") in FINAL or not c.get("started_utc"):
                continue
            if (now - datetime.fromisoformat(c["started_utc"])).total_seconds() < older_than_seconds:
                continue
            try:
                info = self.twilio.get_call(c["call_sid"])
            except Exception as exc:
                log.warning("reconcile %s failed: %s", c["call_sid"], exc)
                continue
            st = (info.get("status") or "").lower()
            if st in FINAL:
                self.status({"CallSid": c["call_sid"], "CallStatus": st,
                             "CallDuration": str(info.get("duration") or "0")})
                done.append({"call_sid": c["call_sid"], "status": st, "duration": info.get("duration")})
        return done

    def kill_live_calls(self) -> List[str]:
        """Kill switch helper: hang up every call that hasn't ended yet."""
        ended = []
        if self.twilio is None:
            return ended
        for c in self.log.calls(200):
            if not c.get("ended") and (c.get("status") or "") not in FINAL:
                try:
                    self.twilio.hangup(c["call_sid"])
                    ended.append(c["call_sid"])
                except Exception as exc:
                    log.warning("hangup %s failed: %s", c["call_sid"], exc)
        return ended

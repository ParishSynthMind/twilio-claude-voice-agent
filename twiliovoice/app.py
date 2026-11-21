"""FastAPI service Twilio calls during a phone call.

Twilio webhooks (form posts, every one must carry a valid X-Twilio-Signature):
- POST /twilio/voice/inbound       the number's "A call comes in" URL: disclosure + first <Gather>
- POST /twilio/voice/outbound      TwiML for a call this service placed, once answered
- POST /twilio/voice/gather        what the caller said (SpeechResult) -> Claude -> next <Gather>
- POST /twilio/voice/pending       comes back for Claude's answer if it took longer than REPLY_WAIT_SECONDS
- POST /twilio/voice/dial-status   result of a transfer to a person
- POST /twilio/voice/status        call status callbacks (duration, end) -> summary email, minutes
Admin (bearer ADMIN_TOKEN): POST /outbound, GET /calls, /leads, /calendar.ics, /guard,
POST /guard/pause (also hangs up live calls), /guard/resume, /reconcile (finish calls whose
final status callback never came, via the REST API). GET /health is open.

Twilio waits only a few seconds for TwiML (5 s on trial accounts), so Claude runs in a worker
thread: if it answers within REPLY_WAIT_SECONDS the answer goes straight back; otherwise the caller
hears "One moment", Twilio pauses and comes back to /pending for the same answer. Pending answers
live in this process, so run ONE worker process.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
from typing import Callable, Dict, Optional, Tuple

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from . import twilio_signature
from .brain import ClaudeBrain, FakeBrain, TurnResult
from .calendar.local import LocalCalendar
from .calllog import CallLog
from .config import Settings
from .costguard import CostGuard, OutboundRefused
from .heartbeat import Heartbeat, default_alert
from .notify import CopyingNotifier, Notifier, OutboxNotifier, SmtpNotifier
from .scheduling import Scheduler
from .service import PATHS, PhoneAgent
from .tools import VoiceAgentTools
from .twilio_api import TwilioError, TwilioVoiceClient

log = logging.getLogger("twiliovoice")
MAX_BODY = 64 * 1024
XML = "application/xml"


def _alert(job: str) -> Callable[[str, str], None]:
    """Failure alerts go through the heartbeat template's alert route (webhook / email / stderr)."""
    def send(subject: str, detail: str) -> None:
        try:
            default_alert(job, "failure", subject, detail)
        except Exception:  # an alert must never break a call
            log.exception("alert failed")
    return send


def build_agent(settings: Settings, brain=None, calendar=None, notifier: Optional[Notifier] = None,
                twilio: Optional[TwilioVoiceClient] = None, alert=None, heartbeat=None) -> PhoneAgent:
    if calendar is None:
        if settings.calendar_backend == "google":
            from .calendar.google import GoogleCalendar, service_account_token_provider
            calendar = GoogleCalendar(settings.google_calendar_id,
                                      service_account_token_provider(settings.google_service_account_file),
                                      timezone=settings.timezone)
        else:
            calendar = LocalCalendar(settings.data_dir / "calendar.sqlite3")
    if notifier is None:
        if settings.smtp_host and settings.team_email:
            notifier = CopyingNotifier(SmtpNotifier(settings.smtp_host, settings.smtp_port, settings.smtp_user,
                                                    settings.smtp_password, settings.smtp_from, settings.team_email),
                                       OutboxNotifier(settings.data_dir / "outbox.jsonl"))
        else:
            notifier = OutboxNotifier(settings.data_dir / "outbox.jsonl")
    alert = alert or _alert("Twilio phone agent")
    calllog = CallLog(settings.data_dir / "calls.sqlite3")
    scheduler = Scheduler(calendar, settings.timezone, settings.hours, settings.slot_minutes,
                          settings.min_notice_minutes)
    guard = CostGuard(calllog, settings.timezone, settings.daily_minutes_budget, settings.daily_call_limit,
                      settings.paused, alert)
    tools = VoiceAgentTools(scheduler, calendar, notifier, calllog, settings.business_name, guard,
                            settings.handover_notice, on_error=alert)
    if brain is None:
        kb = settings.knowledge_base.read_text() if settings.knowledge_base.exists() else ""
        brain = ClaudeBrain(settings.business_name, settings.agent_name, kb, settings.timezone, settings.model,
                            settings.effort, settings.max_output_tokens)
    if twilio is None and settings.twilio_account_sid and settings.twilio_auth_token:
        twilio = TwilioVoiceClient(settings.twilio_account_sid, settings.twilio_auth_token)
    if heartbeat is None:
        heartbeat = Heartbeat("Twilio phone agent", state_dir=str(settings.data_dir / "heartbeat"))
    return PhoneAgent(settings, brain, tools, calllog, guard, notifier, twilio, alert, heartbeat)


def create_app(settings: Optional[Settings] = None, agent: Optional[PhoneAgent] = None,
               fake_brain: bool = False) -> FastAPI:
    settings = settings or Settings.from_env()
    agent = agent or build_agent(settings, brain=FakeBrain() if fake_brain else None)
    app = FastAPI(title="Twilio Claude voice agent", docs_url=None, redoc_url=None)
    app.state.agent = agent
    pending: Dict[str, asyncio.Task] = {}
    app.state.pending = pending

    async def form(request: Request, name: str) -> Dict[str, str]:
        body = await request.body()
        if len(body) > MAX_BODY:
            raise HTTPException(413, "body too large")
        if settings.allow_unsigned:
            return dict(twilio_signature.parse_form(body))
        if not settings.twilio_auth_token or not settings.public_base_url:
            raise HTTPException(503, "TWILIO_AUTH_TOKEN and PUBLIC_BASE_URL must be set to accept Twilio webhooks")
        # Twilio signs the exact public URL it called, query string included.
        url = settings.public_base_url + PATHS[name] + (f"?{request.url.query}" if request.url.query else "")
        try:
            params = twilio_signature.verify(settings.twilio_auth_token, url, body, dict(request.headers))
        except twilio_signature.TwilioSignatureError as exc:
            log.warning("rejected %s: %s", name, exc)
            raise HTTPException(403, "invalid signature") from exc
        if settings.twilio_account_sid and dict(params).get("AccountSid") not in (None, settings.twilio_account_sid):
            raise HTTPException(403, "wrong account")
        return dict(params)

    def twiml(text: str) -> Response:
        return Response(content=text, media_type=XML)

    def admin(request: Request) -> None:
        token = settings.admin_token
        got = request.headers.get("authorization", "")
        if not token or not hmac.compare_digest(got.encode(), f"Bearer {token}".encode()):
            raise HTTPException(401, "admin token required")

    async def wait_for(task: asyncio.Task) -> Tuple[bool, Optional[TurnResult]]:
        try:
            return True, await asyncio.wait_for(asyncio.shield(task), timeout=settings.reply_wait_seconds)
        except asyncio.TimeoutError:
            return False, None

    @app.post(PATHS["inbound"])
    async def inbound(request: Request):
        return twiml(agent.inbound(await form(request, "inbound")))

    @app.post(PATHS["outbound"])
    async def outbound(request: Request):
        return twiml(agent.outbound_answered(await form(request, "outbound")))

    @app.post(PATHS["gather"])
    async def gather(request: Request):
        p = await form(request, "gather")
        if not (p.get("SpeechResult") or "").strip():
            return twiml(agent.empty_speech(p))
        early = agent.record_speech(p)
        if early:
            return twiml(early)
        sid = p.get("CallSid", "")
        task = asyncio.create_task(asyncio.to_thread(agent.think, p))
        pending[sid] = task
        done, result = await wait_for(task)
        if done:
            pending.pop(sid, None)
            return twiml(agent.answer(p, result))
        return twiml(agent.still_thinking(p, 0))

    @app.post(PATHS["pending"])
    async def pending_answer(request: Request):
        p = await form(request, "pending")
        sid = p.get("CallSid", "")
        try:
            round_ = int(request.query_params.get("round", "1"))
        except ValueError:
            round_ = 1
        task = pending.get(sid)
        if task is None:  # nothing waiting (restart, or a duplicate request): ask again rather than go silent
            return twiml(agent.empty_speech(p))
        done, result = await wait_for(task)
        if done:
            pending.pop(sid, None)
            return twiml(agent.answer(p, result))
        if round_ >= settings.max_pending_rounds:
            task.cancel()
            pending.pop(sid, None)
        return twiml(agent.still_thinking(p, round_))

    @app.post(PATHS["dial"])
    async def dial_status(request: Request):
        return twiml(agent.dial_status(await form(request, "dial")))

    @app.post(PATHS["status"])
    async def status(request: Request):
        p = await form(request, "status")
        try:
            agent.status(p)
        except Exception as exc:
            log.exception("status callback failed")
            agent.alert and agent.alert("Phone agent status callback failed", f"{type(exc).__name__}: {exc}")
            raise HTTPException(500, "processing failed; Twilio may retry") from exc
        return PlainTextResponse("ok")

    @app.post("/outbound")
    async def place_outbound(request: Request):
        admin(request)
        body = await request.json()
        try:
            return agent.place_call(str(body.get("to", "")), str(body.get("purpose", "")))
        except OutboundRefused as exc:
            return JSONResponse({"refused": str(exc)}, status_code=422)
        except TwilioError as exc:
            agent.alert and agent.alert("Outbound call failed", str(exc))
            return JSONResponse({"twilio_error": exc.code, "title": exc.title, "detail": exc.detail},
                                status_code=502)

    @app.post("/reconcile")
    async def reconcile(request: Request):
        admin(request)
        return {"finished": await asyncio.to_thread(agent.reconcile, int(request.query_params.get("older_than", "120")))}

    @app.get("/calls")
    async def calls(request: Request):
        admin(request)
        return agent.log.calls()

    @app.get("/leads")
    async def leads(request: Request):
        admin(request)
        return agent.log.leads()

    @app.get("/calendar.ics")
    async def calendar_ics(request: Request):
        admin(request)
        cal = agent.tools.calendar
        if not hasattr(cal, "export_ics"):
            raise HTTPException(404, "only the local calendar exports .ics")
        return PlainTextResponse(cal.export_ics(settings.business_name), media_type="text/calendar")

    @app.get("/guard")
    async def guard_state(request: Request):
        admin(request)
        st = agent.guard.state()
        return {"allowed": st.allowed, "reason": st.reason, "minutes_today": st.minutes_today,
                "calls_today": st.calls_today, "budget_minutes": settings.daily_minutes_budget,
                "call_limit": settings.daily_call_limit}

    @app.post("/guard/pause")
    async def pause(request: Request):
        admin(request)
        agent.guard.pause()
        return {"paused": True, "hung_up": agent.kill_live_calls()}

    @app.post("/guard/resume")
    async def resume(request: Request):
        admin(request)
        agent.guard.resume()
        return {"paused": False}

    @app.get("/health")
    async def health():
        return {"ok": True, "signed_webhooks": not settings.allow_unsigned, "trial_mode": settings.trial_mode,
                "model": settings.model, "paused": agent.guard.paused}

    return app


def _dumps(obj) -> str:  # pragma: no cover - debugging helper
    return json.dumps(obj, indent=2, default=str)

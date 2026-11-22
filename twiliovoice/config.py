from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import secrets_store
from .disclosure import first_message, handover_notice, resolve_disclosure

# Weekday (0 = Monday) -> list of (open, close) in "HH:MM", business local time.
DEFAULT_HOURS: Dict[int, List[Tuple[str, str]]] = {
    0: [("09:00", "17:00")],
    1: [("09:00", "17:00")],
    2: [("09:00", "17:00")],
    3: [("09:00", "17:00")],
    4: [("09:00", "17:00")],
    5: [("10:00", "14:00")],
    6: [],
}

# Pinned platform versions (Vibe Coding Playbook rule B17). Each is ONE config value; change on purpose.
TWILIO_API_BASE = "https://api.twilio.com/2010-04-01"   # Twilio's REST API version is in the path
DEFAULT_MODEL = "claude-sonnet-5-5"
DEFAULT_TTS_VOICE = "Polly.Amy"        # Amazon Polly voice through Twilio <Say>; British English
DEFAULT_SPEECH_MODEL = "default"       # Twilio's generic model (fails over between providers)

# Where outbound calls may go, and when. Calls to UAE numbers are OFF unless the client switches them
# on: UAE Cabinet Resolutions 56 and 57 of 2024 limit marketing calls to 09:00-18:00, from numbers
# registered to the company, never to numbers on the TDRA Do Not Call Register. See docs/REGULATORY.md.
DEFAULT_CALLING_WINDOWS = {"+44": ("Europe/London", "09:00", "20:00"),
                           "+971": ("Asia/Dubai", "09:00", "18:00")}


def _bool(value: Optional[str], default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    business_name: str = "Harbour Dental Clinic"
    agent_name: str = "Cimo"
    timezone: str = "Europe/London"
    slot_minutes: int = 30
    min_notice_minutes: int = 60
    hours: Dict[int, List[Tuple[str, str]]] = field(default_factory=lambda: dict(DEFAULT_HOURS))
    data_dir: Path = Path("var")
    knowledge_base: Path = Path(__file__).resolve().parent.parent / "data" / "knowledge_base.md"

    # Twilio
    public_base_url: str = ""               # the https address Twilio calls (signatures are computed on it)
    twilio_account_sid: Optional[str] = None
    twilio_auth_token: Optional[str] = None
    twilio_from_number: str = ""            # the Twilio number outbound calls come from
    trial_mode: bool = False                # Twilio trial: <Dial> to a number is blocked, so no live transfer

    # Security
    allow_unsigned: bool = False            # local development only; never on a public server
    # Twilio trial "Try out Voice > Receive a call > Custom" posts WITHOUT X-Twilio-Signature (seen live
    # 2026-11-22). With this on (trial mode only), an unsigned webhook is accepted only if its CallSid is a
    # live call on OUR account, fetched from the REST API, with the same From/To.
    trial_verify_unsigned: bool = False
    admin_token: Optional[str] = None       # bearer token for /calls, /leads, /calendar.ics, /guard, /outbound

    # Brain
    model: str = DEFAULT_MODEL
    effort: str = "low"                     # phone turns must be quick
    max_output_tokens: int = 600
    reply_wait_seconds: float = 3.0         # answer inside this, or say "one moment" and come back
    max_pending_rounds: int = 3             # how many "one moment"s before an apology and a handover

    # Speech (Twilio <Gather input="speech"> and <Say>)
    language: str = "en-GB"
    tts_voice: str = DEFAULT_TTS_VOICE
    speech_model: str = DEFAULT_SPEECH_MODEL
    speech_timeout: str = "auto"            # Twilio: a specific speechModel needs a whole number here
    record_calls: bool = False              # Twilio call recording; also picks the recording notice

    # Cost guard
    max_call_seconds: int = 300             # every call ends at this length (TimeLimit + our own check)
    max_turns: int = 12                     # caller answers per call
    daily_call_limit: int = 50
    daily_minutes_budget: int = 120         # sum of billed minutes per day (from Twilio's status callbacks)
    paused: bool = False                    # kill switch: no AI, no outbound, callers get a callback promise

    # Outbound rules
    outbound_allowed_prefixes: Tuple[str, ...] = ("+44",)
    calling_windows: Dict[str, Tuple[str, str, str]] = field(default_factory=lambda: dict(DEFAULT_CALLING_WINDOWS))

    # Hand-over to a person
    human_number: str = ""                  # a real person's phone; empty = email alert + callback promise

    # Calendar and email
    calendar_backend: str = "local"
    google_calendar_id: Optional[str] = None
    google_service_account_file: Optional[str] = None
    team_email: Optional[str] = None
    smtp_host: Optional[str] = None
    smtp_port: int = 587
    smtp_user: Optional[str] = None
    smtp_password: Optional[str] = None
    smtp_from: Optional[str] = None

    # AI disclosure (EU AI Act Art. 50). See twiliovoice/disclosure.py.
    ai_disclosure: str = ""
    ai_disclosure_removed_by_written_request: str = ""

    @property
    def disclosure(self) -> str:
        return resolve_disclosure(self.ai_disclosure, self.ai_disclosure_removed_by_written_request,
                                  self.agent_name, self.business_name)

    @property
    def outbound_disclosure(self) -> str:
        return resolve_disclosure(self.ai_disclosure, self.ai_disclosure_removed_by_written_request,
                                  self.agent_name, self.business_name, outbound=True)

    @property
    def handover_notice(self) -> str:
        return handover_notice(self.ai_disclosure_removed_by_written_request)

    @property
    def first_message(self) -> str:
        return first_message(self.disclosure, self.record_calls)

    def outbound_first_message(self, purpose: str) -> str:
        return first_message(self.outbound_disclosure, self.record_calls,
                             f"I'm calling about {purpose}. Is now a good time?")

    def __post_init__(self) -> None:
        # Summary, lead and handover emails go to ONE fixed address from the config. Nothing a caller
        # says, and nothing the model puts in a tool call, can change the recipient.
        if self.team_email and ("," in self.team_email or ";" in self.team_email or "@" not in self.team_email):
            raise ValueError("TEAM_EMAIL must be a single email address")
        if not 30 <= self.max_call_seconds <= 600:
            raise ValueError("MAX_CALL_SECONDS must be between 30 and 600")
        if not 1 <= self.max_turns <= 50:
            raise ValueError("MAX_TURNS must be between 1 and 50")
        if self.daily_call_limit < 1 or self.daily_minutes_budget < 1:
            raise ValueError("DAILY_CALL_LIMIT and DAILY_MINUTES_BUDGET must be at least 1 (use PAUSED=1 to stop)")
        if self.model.endswith("-latest") or "preview" in self.model:
            raise ValueError("Use a stable model id, never a preview or -latest alias (playbook rule B10)")
        if self.effort not in ("low", "medium", "high"):
            raise ValueError("EFFORT must be low, medium or high (phone turns must stay quick)")
        if self.speech_model != "default" and self.speech_timeout == "auto":
            raise ValueError("Twilio: a specific SPEECH_MODEL needs SPEECH_TIMEOUT as whole seconds, not auto")
        if self.public_base_url and not self.public_base_url.startswith("https://"):
            raise ValueError("PUBLIC_BASE_URL must be https (Twilio signs the exact public URL)")
        self.public_base_url = self.public_base_url.rstrip("/")
        for p in self.outbound_allowed_prefixes:
            if not p.startswith("+") or not p[1:].isdigit():
                raise ValueError(f"OUTBOUND_ALLOWED_PREFIXES entries look like +44, got {p!r}")
        for number in (self.human_number, self.twilio_from_number):
            if number and (not number.startswith("+") or not number[1:].isdigit()):
                raise ValueError("Phone numbers must be in E.164 form, e.g. +447700900123")

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ.get
        prefixes = tuple(p.strip() for p in env("OUTBOUND_ALLOWED_PREFIXES", "+44").split(",") if p.strip())
        return cls(
            business_name=env("BUSINESS_NAME", cls.business_name),
            agent_name=env("AGENT_NAME", cls.agent_name),
            timezone=env("BUSINESS_TZ", cls.timezone),
            slot_minutes=int(env("SLOT_MINUTES", str(cls.slot_minutes))),
            min_notice_minutes=int(env("MIN_NOTICE_MINUTES", str(cls.min_notice_minutes))),
            data_dir=Path(env("DATA_DIR", "var")),
            public_base_url=env("PUBLIC_BASE_URL", ""),
            # Keys: environment first, then the macOS Keychain item named in *_KEYCHAIN. Never a file.
            twilio_account_sid=secrets_store.get("TWILIO_ACCOUNT_SID", env("TWILIO_SID_KEYCHAIN")),
            twilio_auth_token=secrets_store.get("TWILIO_AUTH_TOKEN", env("TWILIO_TOKEN_KEYCHAIN")),
            twilio_from_number=env("TWILIO_FROM_NUMBER", ""),
            trial_mode=_bool(env("TWILIO_TRIAL"), False),
            allow_unsigned=env("ALLOW_UNSIGNED", "") == "1",
            trial_verify_unsigned=_bool(env("TRIAL_VERIFY_UNSIGNED"), False),
            admin_token=env("ADMIN_TOKEN"),
            model=env("CLAUDE_MODEL", cls.model),
            effort=env("EFFORT", cls.effort),
            reply_wait_seconds=float(env("REPLY_WAIT_SECONDS", str(cls.reply_wait_seconds))),
            language=env("SPEECH_LANGUAGE", cls.language),
            tts_voice=env("TTS_VOICE", cls.tts_voice),
            speech_model=env("SPEECH_MODEL", cls.speech_model),
            speech_timeout=env("SPEECH_TIMEOUT", cls.speech_timeout),
            record_calls=_bool(env("RECORD_CALLS"), False),
            max_call_seconds=int(env("MAX_CALL_SECONDS", str(cls.max_call_seconds))),
            max_turns=int(env("MAX_TURNS", str(cls.max_turns))),
            daily_call_limit=int(env("DAILY_CALL_LIMIT", str(cls.daily_call_limit))),
            daily_minutes_budget=int(env("DAILY_MINUTES_BUDGET", str(cls.daily_minutes_budget))),
            paused=_bool(env("PAUSED"), False),
            outbound_allowed_prefixes=prefixes,
            human_number=env("HUMAN_NUMBER", ""),
            calendar_backend=env("CALENDAR_BACKEND", "local"),
            google_calendar_id=env("GOOGLE_CALENDAR_ID"),
            google_service_account_file=env("GOOGLE_SERVICE_ACCOUNT_FILE"),
            team_email=env("TEAM_EMAIL"),
            smtp_host=env("SMTP_HOST"),
            smtp_port=int(env("SMTP_PORT", "587")),
            smtp_user=env("SMTP_USER"),
            smtp_password=secrets_store.get("SMTP_PASSWORD", env("SMTP_PASSWORD_KEYCHAIN")),
            smtp_from=env("SMTP_FROM") or env("SMTP_USER"),
            ai_disclosure=env("AI_DISCLOSURE", ""),
            ai_disclosure_removed_by_written_request=env("AI_DISCLOSURE_REMOVED_BY_WRITTEN_REQUEST", ""),
        )

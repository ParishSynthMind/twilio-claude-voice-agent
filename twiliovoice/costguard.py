"""Cost guard: per-call length and turn caps, a daily call cap and a daily minutes budget, the
outbound rules (allowed countries, calling hours), plus a kill switch.

Layers, so one missing layer never means an open tap:
1. Twilio ends every outbound call at `max_call_seconds` (TimeLimit on the Create Call request).
2. On every turn this server checks the call's age and turn count and wraps the call up politely
   when either cap is reached (inbound calls have no TimeLimit, so this is their limit).
3. Twilio's status callbacks report each call's duration; once today's billed minutes reach
   `daily_minutes_budget`, or calls pass `daily_call_limit`, the AI stops taking calls (callers get
   a callback promise and the team an alert) and no outbound call is placed. One alert per day.
4. `paused` (PAUSED=1, or POST /guard/pause) is the kill switch for everything.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from typing import Callable, Dict, Optional, Tuple
from zoneinfo import ZoneInfo

from .calllog import CallLog


@dataclass
class GuardState:
    allowed: bool
    reason: str
    minutes_today: int
    calls_today: int


class CostGuard:
    def __init__(self, log: CallLog, timezone: str, daily_minutes_budget: int, daily_call_limit: int,
                 paused: bool = False, alert: Optional[Callable[[str, str], None]] = None,
                 now: Optional[Callable[[], datetime]] = None):
        self.log = log
        self.tz = ZoneInfo(timezone)
        self.daily_minutes_budget = daily_minutes_budget
        self.daily_call_limit = daily_call_limit
        self.paused = paused
        self.alert = alert
        self._now = now or (lambda: datetime.now(self.tz))
        self._alerted_day: Optional[str] = None

    def now(self) -> datetime:
        return self._now()

    def today(self) -> str:
        return self._now().astimezone(self.tz).date().isoformat()

    def state(self, starting_new_call: bool = False) -> GuardState:
        day = self.today()
        minutes, calls = self.log.minutes_on(day), self.log.calls_on(day)
        if self.paused:
            return GuardState(False, "paused (kill switch)", minutes, calls)
        if minutes >= self.daily_minutes_budget:
            return GuardState(False, f"daily minutes budget reached ({minutes} of {self.daily_minutes_budget})",
                              minutes, calls)
        limit_hit = calls >= self.daily_call_limit if starting_new_call else calls > self.daily_call_limit
        if limit_hit:
            return GuardState(False, f"daily call limit reached ({calls} of {self.daily_call_limit})", minutes, calls)
        return GuardState(True, "ok", minutes, calls)

    def check(self, starting_new_call: bool = False) -> GuardState:
        """State, plus ONE alert per day the first time the guard trips for a budget or limit."""
        st = self.state(starting_new_call)
        if not st.allowed and not self.paused and self._alerted_day != self.today():
            self._alerted_day = self.today()
            if self.alert:
                self.alert("Phone agent cost guard tripped",
                           f"The phone agent stopped taking calls: {st.reason}. Calls today: {st.calls_today}. "
                           "Raise DAILY_MINUTES_BUDGET / DAILY_CALL_LIMIT or wait for tomorrow.")
        return st

    def pause(self) -> None:
        self.paused = True

    def resume(self) -> None:
        self.paused = False


class OutboundRefused(Exception):
    """An outbound call the rules do not allow. The message says why, in plain words."""


def check_outbound(to: str, allowed_prefixes: Tuple[str, ...], windows: Dict[str, Tuple[str, str, str]],
                   now_utc: datetime) -> None:
    """Raise OutboundRefused unless `to` is an E.164 number in an allowed country, inside that
    country's calling window (the RECIPIENT's local time)."""
    if not to.startswith("+") or not to[1:].isdigit() or not 8 <= len(to) <= 16:
        raise OutboundRefused("The number must be in international form, e.g. +447700900123.")
    prefix = next((p for p in sorted(allowed_prefixes, key=len, reverse=True) if to.startswith(p)), None)
    if prefix is None:
        if to.startswith("+971"):
            raise OutboundRefused("Calls to UAE numbers are switched off. UAE rules (Cabinet Resolutions 56 and 57 "
                                  "of 2024) need a company-registered number, 09:00-18:00 only and a Do Not Call "
                                  "Register check first. Add +971 to OUTBOUND_ALLOWED_PREFIXES only once those are in place.")
        raise OutboundRefused(f"Calls to {to[:4]}... are not in OUTBOUND_ALLOWED_PREFIXES.")
    window = next((w for p, w in sorted(windows.items(), key=lambda kv: len(kv[0]), reverse=True)
                   if to.startswith(p)), None)
    if window:
        tz, start, end = window
        local = now_utc.astimezone(ZoneInfo(tz))
        if not time.fromisoformat(start) <= local.time() < time.fromisoformat(end):
            raise OutboundRefused(f"It is {local:%H:%M} for the person called ({tz}); calls are only placed "
                                  f"between {start} and {end} their time.")

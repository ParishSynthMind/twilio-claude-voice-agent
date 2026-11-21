"""The tools Claude can call during a phone call (check_availability, book_appointment, capture_lead,
handover, end_call).

Each returns a short sentence the agent can speak. The model never chooses who gets an email,
never sees another caller's data, and a cost-guard trip turns every action into "take a message".
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Callable, Dict, Optional

from .calendar.base import Booking, CalendarAdapter
from .calllog import CallLog
from .costguard import CostGuard
from .disclosure import DEFAULT_HANDOVER
from .notify import Notifier
from .scheduling import Scheduler, parse_time, resolve_day, spoken

EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[A-Za-z]{2,24}$")
GUARD_MESSAGE = ("I can't make bookings or changes right now. Offer to take the caller's name and number "
                 "so the team can call back, and use the handover tool for that.")


def _clean(value: Any, limit: int = 200) -> str:
    """Caller-supplied text goes into calendars and emails: strip control characters and cap the length."""
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or "")).strip()
    return text[:limit]


class ToolError(Exception):
    """A problem the caller should hear about in plain words (bad date, slot taken), not a crash."""


class VoiceAgentTools:
    def __init__(self, scheduler: Scheduler, calendar: CalendarAdapter, notifier: Notifier, log: CallLog,
                 business_name: str, guard: CostGuard, handover_notice: str = DEFAULT_HANDOVER,
                 on_error: Optional[Callable[[str, str], None]] = None):
        self.scheduler = scheduler
        self.calendar = calendar
        self.notifier = notifier
        self.log = log
        self.business_name = business_name
        self.guard = guard
        self.handover_notice = handover_notice
        self.on_error = on_error
        self.handlers: Dict[str, Callable[..., str]] = {
            "check_availability": self.check_availability,
            "book_appointment": self.book_appointment,
            "capture_lead": self.capture_lead,
            "handover": self.handover,
            "end_call": self.end_call,
        }
        # Handover still works when the guard has tripped: a person must always be reachable.
        self.guarded = {"book_appointment", "capture_lead", "check_availability"}

    def _add_outcome(self, call_id: Optional[str], text: str, **fields: Any) -> None:
        self.log.add_outcome(call_id, text, **fields)

    def run(self, name: str, args: Dict[str, Any], call: Dict[str, Any]) -> str:
        call_id = call.get("id")
        handler = self.handlers.get(name)
        if handler is None:
            result = f"Unknown tool {name}."
        elif name in self.guarded and not self.guard.check().allowed:
            result = GUARD_MESSAGE
        else:
            try:
                result = handler(args, call)
            except ToolError as e:
                result = str(e)
            except Exception as e:  # never leave the caller in silence; log, alert and say something useful
                self.log.tool_event(call_id, name, args, f"ERROR {type(e).__name__}")
                if self.on_error:
                    self.on_error(f"Phone agent tool {name} failed", f"{type(e).__name__}: {e} (call {call_id})")
                return ("Sorry, I couldn't reach the booking system just now. "
                        "Offer to take a message and have the team call back.")
        self.log.tool_event(call_id, name, args, result)
        return result

    # check_availability --------------------------------------------------------------------------
    def check_availability(self, args: Dict[str, Any], call: Dict[str, Any]) -> str:
        now = self.scheduler.now()
        today = now.date()
        day = resolve_day(args.get("date"), today)
        if args.get("date") and day is None:
            raise ToolError(f"I couldn't work out the date '{_clean(args.get('date'), 40)}'. "
                            f"Ask the caller for a day of the week or a date. Today is {now:%A %d %B %Y}.")
        if day is not None and day < today:
            raise ToolError(f"That date has passed. Today is {now:%A %d %B %Y}.")
        if day is not None:
            slots = self.scheduler.free_slots(day, now)
            if slots:
                def times(part):
                    return ", ".join(spoken(s).split(" at ")[1] for s in part) or "none"
                morning = [s for s in slots if s.hour < 12]
                afternoon = [s for s in slots if s.hour >= 12]
                return (f"Today is {now:%A %d %B %Y}. Free 30-minute slots on {day:%A %d %B} "
                        f"(date {day.isoformat()}). Morning: {times(morning)}. Afternoon: {times(afternoon)}.")
            alt = next(self.scheduler.next_days_with_slots(day, 14, now), None)
            if alt is None:
                return f"Today is {now:%A %d %B %Y}. Nothing free on {day:%A %d %B} or in the two weeks after."
            d, s = alt
            return (f"Today is {now:%A %d %B %Y}. Nothing free on {day:%A %d %B}. The next free day is "
                    f"{d:%A %d %B} (date {d.isoformat()}): " + ", ".join(spoken(x).split(" at ")[1] for x in s[:6]) + ".")
        found = list(self.scheduler.next_days_with_slots(today, 7, now))[:3]
        if not found:
            return f"Today is {now:%A %d %B %Y}. No free slots in the next week."
        parts = [f"{d:%A %d %B} (date {d.isoformat()}): " + ", ".join(spoken(x).split(' at ')[1] for x in s[:4])
                 for d, s in found]
        return f"Today is {now:%A %d %B %Y}. Next free slots. " + "; ".join(parts) + "."

    # book_appointment ----------------------------------------------------------------------------
    def book_appointment(self, args: Dict[str, Any], call: Dict[str, Any]) -> str:
        now = self.scheduler.now()
        name = _clean(args.get("name"), 80)
        phone = _clean(args.get("phone") or call.get("caller"), 40)
        reason = _clean(args.get("reason") or "Appointment", 120)
        day = resolve_day(args.get("date"), now.date())
        at = parse_time(str(args.get("time") or ""))
        if not name:
            raise ToolError("I need the caller's name before booking.")
        if not phone:
            raise ToolError("I need a contact phone number before booking.")
        if day is None or at is None:
            raise ToolError("I need a date and a time for the booking, as YYYY-MM-DD and HH:MM.")
        start = datetime.combine(day, at, self.scheduler.tz)
        if not self.scheduler.is_free(start, now):
            raise ToolError(f"{spoken(start)} is not available. Check availability again and offer another time.")
        booking = Booking(start=start, end=start + self.scheduler.slot, name=name, phone=phone, reason=reason,
                          call_id=call.get("id"))
        booking.event_id = self.calendar.create_event(booking)
        self._add_outcome(call.get("id"), f"Booked {spoken(start)} for {name} ({reason})")
        return (f"Booked. {name}, {reason}, {spoken(start)}. Reference {booking.event_id[:6].upper()}. "
                f"Confirm this back to the caller.")

    # capture_lead --------------------------------------------------------------------------------
    def capture_lead(self, args: Dict[str, Any], call: Dict[str, Any]) -> str:
        name = _clean(args.get("name"), 80)
        phone = _clean(args.get("phone") or call.get("caller"), 40)
        email = _clean(args.get("email"), 254)
        interest = _clean(args.get("interest") or "General enquiry", 300)
        consent = args.get("consent_to_contact") is True or str(args.get("consent_to_contact")).lower() == "true"
        if not name:
            raise ToolError("I need the caller's name to pass their details on.")
        if not phone and not email:
            raise ToolError("I need a phone number or an email address so the team can get back to them.")
        if email and not EMAIL.match(email):
            raise ToolError("That email address doesn't look complete. Ask the caller to spell it again.")
        if not consent:
            raise ToolError("Ask the caller whether the team may contact them about this, and only save the "
                            "details if they say yes.")
        stored = self.log.add_lead(call.get("id"), name, phone, email, interest, consent)
        if stored:
            body = (f"New lead from the {self.business_name} phone agent\n\nName: {name}\nPhone: {phone or 'not given'}\n"
                    f"Email: {email or 'not given'}\nInterested in: {interest}\nConsent to contact: yes\n"
                    f"Call id: {call.get('id') or 'n/a'}\n")
            self.notifier.send(f"New lead: {name}", body)
            self._add_outcome(call.get("id"), f"Lead captured: {interest}")
        return f"Saved. The team will contact {name} about {interest}. Tell the caller that."

    # handover ------------------------------------------------------------------------------------
    def handover(self, args: Dict[str, Any], call: Dict[str, Any]) -> str:
        caller = _clean(args.get("caller_name") or "Unknown caller", 80)
        phone = _clean(args.get("callback_number") or call.get("caller") or "not given", 40)
        reason = _clean(args.get("reason") or "Caller asked for a person", 400)
        urgent = args.get("urgent") is True or str(args.get("urgent")).lower() == "true"
        body = (f"A caller asked to speak to a person.\n\nCaller: {caller}\nCall back on: {phone}\n"
                f"Urgent: {'YES' if urgent else 'no'}\nReason: {reason}\nCall id: {call.get('id') or 'n/a'}\n")
        self.notifier.send(f"{'URGENT ' if urgent else ''}Handover: call back {caller}", body)
        self._add_outcome(call.get("id"), f"Handed over: {reason}", handover=1)
        when = "within 15 minutes" if urgent else "within the hour, during opening hours"
        notice = f"{self.handover_notice} " if self.handover_notice else ""
        return (f"{notice}The team has been alerted and will call {caller} back on {phone} {when}. "
                f"Tell the caller exactly that.")

    # end_call ------------------------------------------------------------------------------------
    def end_call(self, args: Dict[str, Any], call: Dict[str, Any]) -> str:
        reason = _clean(args.get("reason") or "Caller finished", 120)
        self._add_outcome(call.get("id"), f"Ended: {reason}")
        return "The call will end after your goodbye. Say a short goodbye now."

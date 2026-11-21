from .unit_fixtures import FIXED_NOW


def run(tools, name, args, call_id="conv_1"):
    return tools.run(name, args, {"id": call_id, "caller": None})


def test_availability_for_a_day(tools):
    out = run(tools, "check_availability", {"date": "2025-11-03"})
    assert "Morning:" in out and "Afternoon:" in out and "2025-11-03" in out
    assert "Today is Monday 03 November 2025" in out


def test_availability_without_date_lists_next_days(tools):
    out = run(tools, "check_availability", {})
    assert out.startswith("Today is Monday") and "Next free slots" in out


def test_availability_past_date(tools):
    assert "has passed" in run(tools, "check_availability", {"date": "2025-10-01"})


def test_availability_bad_date(tools):
    assert "couldn't work out the date" in run(tools, "check_availability", {"date": "the blue moon"})


def test_sunday_closed_offers_next_day(tools):
    out = run(tools, "check_availability", {"date": "2025-11-09"})
    assert "Nothing free on Sunday" in out and "next free day" in out


def test_book_then_double_book_refused(tools, calendar):
    args = {"name": "Ana Silva", "phone": "07700900123", "date": "2025-11-04", "time": "10:00", "reason": "check-up"}
    first = run(tools, "book_appointment", args)
    assert first.startswith("Booked.") and "Reference" in first
    second = run(tools, "book_appointment", dict(args, name="Someone Else"))
    assert "not available" in second


def test_book_needs_name_phone_date(tools):
    assert "name" in run(tools, "book_appointment", {"phone": "1", "date": "2025-11-04", "time": "10:00"})
    assert "phone" in run(tools, "book_appointment", {"name": "A", "date": "2025-11-04", "time": "10:00"})
    assert "date and a time" in run(tools, "book_appointment", {"name": "A", "phone": "1", "date": "x"})


def test_book_outside_hours_refused(tools):
    out = run(tools, "book_appointment", {"name": "A", "phone": "1", "date": "2025-11-04", "time": "07:00"})
    assert "not available" in out


def test_booking_logged_as_outcome(tools, calllog):
    run(tools, "book_appointment", {"name": "Ana", "phone": "1", "date": "2025-11-04", "time": "11:00"})
    assert "Booked" in calllog.get("conv_1")["outcome"]


def test_caller_text_cleaned(tools, calendar):
    run(tools, "book_appointment", {"name": "Ana\x00\nDrop", "phone": "1\r\n2", "date": "2025-11-04",
                                    "time": "12:00", "reason": "x" * 500})
    ev = calendar.busy(FIXED_NOW, FIXED_NOW.replace(day=5))
    assert ev  # booked
    row = calendar._db.execute("SELECT name, phone, reason FROM appointments").fetchone()
    assert "\x00" not in row[0] and "\n" not in row[0] and len(row[2]) <= 120


def test_lead_requires_consent(tools, notifier, calllog):
    out = run(tools, "capture_lead", {"name": "Bo", "phone": "0711", "interest": "whitening", "consent_to_contact": False})
    assert "Ask the caller whether" in out
    assert notifier.sent == [] and calllog.leads() == []


def test_lead_saved_and_emailed_once(tools, notifier, calllog):
    args = {"name": "Bo", "phone": "0711", "interest": "whitening", "consent_to_contact": True}
    assert "Saved" in run(tools, "capture_lead", args)
    run(tools, "capture_lead", args)  # model repeats the call
    assert len(calllog.leads()) == 1 and len(notifier.sent) == 1
    assert notifier.sent[0][0] == "New lead: Bo"


def test_lead_consent_as_string(tools, calllog):
    run(tools, "capture_lead", {"name": "Cy", "email": "cy@example.com", "interest": "Invisalign",
                                "consent_to_contact": "true"})
    assert calllog.leads()[0]["email"] == "cy@example.com"


def test_lead_needs_contact_and_valid_email(tools):
    assert "phone number or an email" in run(tools, "capture_lead", {"name": "D", "interest": "x", "consent_to_contact": True})
    assert "doesn't look complete" in run(tools, "capture_lead", {"name": "D", "email": "d@", "interest": "x",
                                                                  "consent_to_contact": True})


def test_lead_needs_name(tools):
    assert "name" in run(tools, "capture_lead", {"phone": "1", "consent_to_contact": True})


def test_handover_urgent(tools, notifier, calllog):
    out = run(tools, "handover", {"caller_name": "Eve", "callback_number": "0722", "reason": "toothache", "urgent": True})
    assert out.startswith("I'm an AI assistant, so I'm passing you to a person") and "within 15 minutes" in out
    assert notifier.sent[0][0].startswith("URGENT Handover")
    assert calllog.get("conv_1")["handover"] == 1


def test_handover_not_urgent(tools, notifier):
    out = run(tools, "handover", {"reason": "billing question", "urgent": "false"})
    assert "within the hour" in out and not notifier.sent[0][0].startswith("URGENT")


def test_unknown_tool(tools):
    assert run(tools, "delete_everything", {}) == "Unknown tool delete_everything."


def test_crash_is_spoken_and_alerted(tools, unit_alerts, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("calendar down")
    monkeypatch.setattr(tools.calendar, "create_event", boom)
    out = run(tools, "book_appointment", {"name": "A", "phone": "1", "date": "2025-11-04", "time": "13:00"})
    assert "couldn't reach the booking system" in out
    assert unit_alerts.sent and "book_appointment" in unit_alerts.sent[0][0]


def test_tool_events_logged(tools, calllog):
    run(tools, "check_availability", {})
    assert calllog.tool_events("conv_1")[0]["tool"] == "check_availability"


def test_two_outcomes_kept(tools, calllog):
    run(tools, "book_appointment", {"name": "A", "phone": "1", "date": "2025-11-04", "time": "14:00"})
    run(tools, "handover", {"reason": "question", "urgent": False})
    outcome = calllog.get("conv_1")["outcome"]
    assert "Booked" in outcome and "Handed over" in outcome

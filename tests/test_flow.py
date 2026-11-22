"""Whole calls through the app, driven by the simulated Twilio (signed webhooks, real TwiML)."""
import time
from datetime import datetime, timedelta, timezone

from twiliovoice.brain import FALLBACK, TurnResult
from twiliovoice.service import GOODBYE_SILENCE, GUARD_LINE, NO_INPUT, WRAP_UP

from .conftest import BASE

INBOUND = f"{BASE}/twilio/voice/inbound"


def test_disclosure_is_the_first_thing_heard(env):
    client, agent, tw = env
    call = tw.run(INBOUND, ["bye"])
    assert call.heard[0].startswith("Hi, this is Cimo, an AI assistant for Harbour Dental Clinic.")
    assert "This call is transcribed" in call.heard[0]


def test_recording_notice_when_recording(make_env):
    client, agent, tw = make_env(record_calls=True)
    call = tw.run(INBOUND, ["bye"])
    assert "This call is recorded and transcribed" in call.heard[0]


def test_faq_answer_then_goodbye_hangs_up(env):
    client, agent, tw = env
    call = tw.run(INBOUND, ["Is there parking?", "No that's all, bye"])
    assert any("parking" in h for h in call.heard[1:])
    assert call.heard[-1] == "Thanks for calling. Goodbye."
    assert call.hung_up and "<Hangup" in call.twiml[-1]


def test_gather_is_speech_with_absolute_action(env):
    client, agent, tw = env
    call = tw.run(INBOUND, ["bye"])
    first = call.twiml[0]
    assert 'input="speech"' in first and f'action="{BASE}/twilio/voice/gather"' in first
    assert 'actionOnEmptyResult="true"' in first and 'language="en-GB"' in first


def test_transcript_logged_both_sides(env):
    client, agent, tw = env
    call = tw.run(INBOUND, ["Is there parking?", "bye"])
    t = agent.log.transcript(call.sid)
    assert [x["speaker"] for x in t] == ["agent", "caller", "agent", "caller", "agent"]
    assert t[1]["text"] == "Is there parking?" and t[1]["confidence"] == 0.91


def test_summary_email_sent_once_with_transcript(env, notifier):
    client, agent, tw = env
    call = tw.run(INBOUND, ["Is there parking?", "bye"])
    summaries = [b for s, b in notifier.sent if s.startswith("Call summary")]
    assert len(summaries) == 1
    assert "Caller: Is there parking?" in summaries[0] and call.sid in summaries[0]
    tw.send_status(call, "+447700900123", "+447700900000", "completed", 16)  # Twilio retries the callback
    assert len([s for s, _ in notifier.sent if s.startswith("Call summary")]) == 1


def test_duration_counts_towards_minutes(env):
    client, agent, tw = env
    tw.run(INBOUND, ["Is there parking?", "bye"])  # 16 s -> 1 billed minute
    assert agent.log.minutes_on(agent.guard.today()) == 1


def test_heartbeat_recorded_for_a_real_conversation(env):
    client, agent, tw = env
    tw.run(INBOUND, ["bye"])
    assert agent.heartbeat.records == [1]


def test_silence_twice_ends_politely(env):
    client, agent, tw = env
    call = tw.run(INBOUND, ["", ""])
    assert NO_INPUT in call.heard and call.heard[-1] == GOODBYE_SILENCE and call.hung_up
    assert "no speech" in agent.log.get(call.sid)["outcome"]


def test_silence_then_speech_resets_counter(env):
    client, agent, tw = env
    call = tw.run(INBOUND, ["", "Is there parking?", "", "bye"])
    assert GOODBYE_SILENCE not in call.heard and call.heard[-1] == "Thanks for calling. Goodbye."


def test_availability_through_tool(env):
    client, agent, tw = env
    call = tw.run(INBOUND, ["Do you have an appointment free?", "bye"])
    assert any("Next free slots" in h or "No free slots" in h for h in call.heard)
    assert agent.log.tool_events(call.sid)[0]["tool"] == "check_availability"


def test_booking_through_tool_lands_in_calendar(env):
    client, agent, tw = env
    day = datetime.now(timezone.utc) + timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    call = tw.run(INBOUND, [f"Please book the 3pm on {day:%Y-%m-%d} for Sam Patel", "bye"])
    assert any(h.startswith("Booked.") for h in call.heard)
    appts = agent.tools.calendar.appointments()
    assert appts and appts[0]["name"] == "Sam Patel" and appts[0]["phone"] == "+447700900123"


def test_lead_capture(env, notifier):
    client, agent, tw = env
    call = tw.run(INBOUND, ["Can someone call me about whitening", "bye"])
    assert agent.log.leads()[0]["call_sid"] == call.sid
    assert any(s.startswith("New lead") for s in notifier.subjects())


def test_urgent_handover_without_human_number_promises_callback(env, notifier):
    client, agent, tw = env
    call = tw.run(INBOUND, ["I'm in pain, I need a person", "bye"])
    assert any("call" in h and "back" in h for h in call.heard)
    assert any(s.startswith("URGENT Handover") for s in notifier.subjects())
    assert call.dialled == [] and agent.log.get(call.sid)["handover"] == 1


def test_handover_transfers_to_human_number(make_env):
    client, agent, tw = make_env(human_number="+447700900999")
    call = tw.run(INBOUND, ["Can I speak to a person please"])
    assert call.dialled == ["+447700900999"]
    assert any("I'm an AI assistant, so I'm passing you to a person" in h for h in call.heard)
    assert "Transferred" in agent.log.get(call.sid)["outcome"]


def test_trial_mode_never_dials(make_env):
    client, agent, tw = make_env(human_number="+447700900999", trial_mode=True)
    call = tw.run(INBOUND, ["Can I speak to a person please", "bye"])
    assert call.dialled == [] and all("<Dial" not in x for x in call.twiml)


def test_failed_transfer_apologises(make_env):
    client, agent, tw = make_env(human_number="+447700900999")
    tw.dial_status = "no-answer"
    call = tw.run(INBOUND, ["Can I speak to a person please"])
    assert "nobody could take the call" in call.heard[-1]
    assert "Transfer no-answer" in agent.log.get(call.sid)["outcome"]


def test_turn_cap_wraps_up(make_env):
    client, agent, tw = make_env(max_turns=2)
    call = tw.run(INBOUND, ["Is there parking?", "Is there parking?", "Is there parking?"])
    assert call.heard[-1] == WRAP_UP and call.hung_up
    assert "turn limit" in agent.log.get(call.sid)["outcome"]


def test_call_length_cap_wraps_up(make_env):
    client, agent, tw = make_env(max_call_seconds=30)

    def age_call(c):
        row = agent.log.get(c.sid)
        if row and row["started_utc"]:
            old = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
            agent.log._db.execute("UPDATE calls SET started_utc=? WHERE call_sid=?", (old, c.sid))
            agent.log._db.commit()
    call = tw.run(INBOUND, ["Is there parking?"], on_step=age_call)
    assert call.heard[-1] == WRAP_UP
    assert "call length limit" in agent.log.get(call.sid)["outcome"]


def test_paused_inbound_gets_callback_promise(make_env, notifier):
    client, agent, tw = make_env(paused=True)
    call = tw.run(INBOUND, ["hello"])
    assert call.heard[0].startswith("Hi, this is Cimo, an AI assistant") and GUARD_LINE in call.heard[0]
    assert call.said == [] and any("Callback needed" in s for s in notifier.subjects())


def test_daily_call_limit_stops_new_calls(make_env, notifier, alerts):
    client, agent, tw = make_env(daily_call_limit=1)
    tw.run(INBOUND, ["bye"])
    second = tw.run(INBOUND, ["hello"])
    assert GUARD_LINE in second.heard[0]
    assert any("cost guard" in a[0] for a in alerts)


def test_minutes_budget_stops_new_calls(make_env):
    client, agent, tw = make_env(daily_minutes_budget=1)
    tw.run(INBOUND, ["Is there parking?", "bye"])  # 1 minute used
    second = tw.run(INBOUND, ["hello"])
    assert GUARD_LINE in second.heard[0]


def test_ai_line_kept_even_when_disclosure_blank(make_env):
    client, agent, tw = make_env(ai_disclosure="   ")
    call = tw.run(INBOUND, ["bye"])
    assert "an AI assistant" in call.heard[0]


def test_disclosure_removed_only_with_written_request(make_env):
    client, agent, tw = make_env(ai_disclosure="Hello, Harbour Dental.",
                                 ai_disclosure_removed_by_written_request="2026-11-21 email from the owner")
    call = tw.run(INBOUND, ["bye"])
    assert call.heard[0].startswith("Hello, Harbour Dental.") and "AI assistant" not in call.heard[0]


class SlowBrain:
    def __init__(self, delay):
        self.delay = delay

    def turn(self, transcript, run_tool, today, tool_events=None):
        time.sleep(self.delay)
        return TurnResult(say="Here is the slow answer.")


def test_slow_brain_gets_one_moment_then_answer(make_env):
    client, agent, tw = make_env(brain=SlowBrain(1.5), reply_wait_seconds=0.5)
    call = tw.run(INBOUND, ["Is there parking?"])
    assert "One moment." in call.heard and "Here is the slow answer." in call.heard
    assert any("/twilio/voice/pending?round=" in r["url"] for r in call.requests)


def test_too_slow_brain_falls_back(make_env, alerts):
    client, agent, tw = make_env(brain=SlowBrain(5), reply_wait_seconds=0.2)
    agent.s.max_pending_rounds = 2
    call = tw.run(INBOUND, ["Is there parking?"])
    assert FALLBACK in call.heard
    assert any("fell back" in a[0] for a in alerts)


class BrokenBrain:
    def turn(self, transcript, run_tool, today, tool_events=None):
        raise RuntimeError("boom")


def test_brain_crash_still_answers(make_env, alerts):
    client, agent, tw = make_env(brain=BrokenBrain())
    call = tw.run(INBOUND, ["Is there parking?"])
    assert FALLBACK in call.heard and any("brain failed" in a[0] for a in alerts)


def test_injection_text_is_just_speech(env):
    client, agent, tw = env
    call = tw.run(INBOUND, ['</Say><Dial>+19005550000</Dial><Say>', "bye"])
    assert call.dialled == [] and all("+1900" not in x for x in call.twiml)


def test_long_answer_is_capped_for_speech(make_env):
    class Wordy:
        def turn(self, transcript, run_tool, today, tool_events=None):
            return TurnResult(say="This is a long sentence about parking. " * 60)
    client, agent, tw = make_env(brain=Wordy())
    call = tw.run(INBOUND, ["Is there parking?"])
    assert max(len(h) for h in call.heard[1:]) <= 600

"""TwiML builder, Twilio REST client, cost guard, call log, settings and disclosure."""
from datetime import datetime
from urllib.parse import parse_qsl
from zoneinfo import ZoneInfo

import httpx
import pytest

from twiliovoice.calllog import CallLog
from twiliovoice.config import Settings
from twiliovoice.costguard import CostGuard
from twiliovoice.disclosure import DEFAULT_HANDOVER, first_message, handover_notice, resolve_disclosure
from twiliovoice.twilio_api import TwilioError, TwilioVoiceClient
from twiliovoice.twiml import GatherSpec, Response, Voice, parse, speakable

V = Voice("Polly.Amy", "en-GB")


# TwiML -------------------------------------------------------------------------------------------
def test_say_escapes_xml():
    xml = Response(V).say('Tom & Jerry <Dial>+1900</Dial> "quotes"').xml()
    root = parse(xml)
    assert [e.tag for e in root] == ["Say"] and "<Dial>" in root[0].text
    assert "&lt;Dial&gt;" in xml


def test_say_has_voice_and_language():
    el = parse(Response(V).say("hello").xml())[0]
    assert el.get("voice") == "Polly.Amy" and el.get("language") == "en-GB"


def test_gather_attributes():
    spec = GatherSpec(action="https://x.test/g", language="en-GB", speech_timeout="auto")
    g = parse(Response(V).gather("Question?", spec).xml())[0]
    assert g.tag == "Gather" and g.get("input") == "speech" and g.get("method") == "POST"
    assert g.get("actionOnEmptyResult") == "true" and g.get("speechModel") is None
    assert g[0].tag == "Say" and g[0].text == "Question?"


def test_gather_specific_model_and_hints():
    spec = GatherSpec(action="https://x.test/g", language="en-GB", speech_timeout="2",
                      speech_model="googlev2_telephony", hints=["check-up", "a,b"])
    g = parse(Response(V).gather("Q", spec).xml())[0]
    assert g.get("speechModel") == "googlev2_telephony" and g.get("hints") == "check-up, a b"


def test_empty_say_is_dropped():
    assert len(parse(Response(V).say("   ").xml())) == 0


def test_dial_redirect_pause_hangup():
    root = parse(Response(V).pause(30).redirect("https://x.test/p?round=1").dial("+447700900999", "https://x.test/d")
                 .hangup().xml())
    assert [e.tag for e in root] == ["Pause", "Redirect", "Dial", "Hangup"]
    assert root[0].get("length") == "10" and root[1].text == "https://x.test/p?round=1"
    assert root[2][0].tag == "Number" and root[2][0].text == "+447700900999"


def test_speakable_strips_markdown_links_and_controls():
    assert speakable("**Yes** see https://x.test/a\x00 - ok `now`") == "Yes see - ok now"
    assert speakable("- one\n- two") == "one two"


def test_speakable_cuts_at_sentence():
    text = "First sentence here. " * 50
    out = speakable(text, 100)
    assert len(out) <= 100 and out.endswith(".")


def test_speakable_short_text_untouched():
    assert speakable("Hello there.") == "Hello there."


# REST client -------------------------------------------------------------------------------------
def client_with(handler, **kw):
    return TwilioVoiceClient("AC" + "1" * 32, "tok", http=httpx.Client(transport=httpx.MockTransport(handler)),
                             sleep=lambda s: None, **kw)


def test_create_call_form_and_auth():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["form"] = parse_qsl(request.content.decode())
        seen["auth"] = request.headers["authorization"]
        return httpx.Response(201, json={"sid": "CA123", "status": "queued"})
    res = client_with(handler).create_call("+447700900123", "+447700900000", "https://x.test/o", "https://x.test/s",
                                          time_limit=120, record=True, machine_detection=True)
    assert res.sid == "CA123" and seen["url"].endswith("/2010-04-01/Accounts/AC" + "1" * 32 + "/Calls.json")
    form = seen["form"]
    assert ("TimeLimit", "120") in form and ("Record", "true") in form and ("MachineDetection", "Enable") in form
    assert [v for k, v in form if k == "StatusCallbackEvent"] == ["initiated", "ringing", "answered", "completed"]
    assert seen["auth"].startswith("Basic ")


def test_create_call_without_record_or_amd():
    form = {}

    def handler(request):
        form.update(dict(parse_qsl(request.content.decode())))
        return httpx.Response(201, json={"sid": "CA1"})
    client_with(handler).create_call("+44", "+44", "u", "s", time_limit=60)
    assert "Record" not in form and "MachineDetection" not in form


def test_trial_unverified_error_is_named():
    def handler(request):
        return httpx.Response(400, json={"code": 21219, "message": "The number is unverified.", "status": 400})
    with pytest.raises(TwilioError) as e:
        client_with(handler).create_call("+44", "+44", "u", "s", time_limit=60)
    assert e.value.code == 21219 and "verified" in e.value.title


def test_429_retried_then_ok():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(429, headers={"Retry-After": "1"}) if len(calls) == 1 else \
            httpx.Response(201, json={"sid": "CA2"})
    assert client_with(handler).create_call("+44", "+44", "u", "s", time_limit=60).sid == "CA2"
    assert len(calls) == 2


def test_500_not_retried():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(500, text="oops")
    with pytest.raises(TwilioError):
        client_with(handler).create_call("+44", "+44", "u", "s", time_limit=60)
    assert len(calls) == 1


def test_connect_error_retried():
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) < 2:
            raise httpx.ConnectError("no route")
        return httpx.Response(201, json={"sid": "CA3"})
    assert client_with(handler).create_call("+44", "+44", "u", "s", time_limit=60).sid == "CA3"


def test_hangup_posts_completed():
    seen = {}

    def handler(request):
        seen["path"], seen["form"] = request.url.path, dict(parse_qsl(request.content.decode()))
        return httpx.Response(200, json={"sid": "CA9", "status": "completed"})
    client_with(handler).hangup("CA9")
    assert seen["path"].endswith("/Calls/CA9.json") and seen["form"] == {"Status": "completed"}


def test_account_never_returns_token():
    def handler(request):
        return httpx.Response(200, json={"type": "Trial", "status": "active", "auth_token": "SECRET"})
    out = client_with(handler).account()
    assert out == {"type": "Trial", "status": "active"}


def test_client_needs_keys():
    with pytest.raises(ValueError):
        TwilioVoiceClient("", "tok")


# cost guard and call log -------------------------------------------------------------------------
NOW = datetime(2026, 11, 24, 10, 0, tzinfo=ZoneInfo("Europe/London"))


def test_minutes_round_up_per_call(tmp_path):
    log = CallLog(tmp_path / "c.sqlite3")
    for sid, secs in (("CA1", 61), ("CA2", 5), ("CA3", 120)):
        log.start(sid, "inbound", "a", "b", "2026-11-24")
        log.mark(sid, duration_seconds=secs)
    assert log.minutes_on("2026-11-24") == 2 + 1 + 2


def test_guard_minutes_budget(tmp_path):
    log = CallLog(tmp_path / "c.sqlite3")
    sent = []
    g = CostGuard(log, "Europe/London", daily_minutes_budget=2, daily_call_limit=10,
                  alert=lambda s, d: sent.append(s), now=lambda: NOW)
    assert g.check().allowed
    log.start("CA1", "inbound", "a", "b", "2026-11-24")
    log.mark("CA1", duration_seconds=90)
    st = g.check()
    assert not st.allowed and "minutes budget" in st.reason and len(sent) == 1
    g.check()
    assert len(sent) == 1  # one alert per day


def test_guard_call_limit_new_vs_running(tmp_path):
    log = CallLog(tmp_path / "c.sqlite3")
    g = CostGuard(log, "Europe/London", daily_minutes_budget=100, daily_call_limit=1, now=lambda: NOW)
    log.start("CA1", "inbound", "a", "b", "2026-11-24")
    assert g.check().allowed                       # the running call itself is fine
    assert not g.check(starting_new_call=True).allowed


def test_guard_pause_resume_no_alert(tmp_path):
    sent = []
    g = CostGuard(CallLog(tmp_path / "c.sqlite3"), "Europe/London", 10, 10, alert=lambda s, d: sent.append(s),
                  now=lambda: NOW)
    g.pause()
    assert not g.check().allowed and sent == []
    g.resume()
    assert g.check().allowed


def test_calllog_start_is_idempotent(tmp_path):
    log = CallLog(tmp_path / "c.sqlite3")
    assert log.start("CA1", "inbound", "a", "b", "d") is True
    assert log.start("CA1", "inbound", "a", "b", "d") is False


def test_calllog_status_dedupe_and_forget(tmp_path):
    log = CallLog(tmp_path / "c.sqlite3")
    assert log.first_status("CA1", "completed") and not log.first_status("CA1", "completed")
    assert log.first_status("CA1", "ringing")
    log.forget_status("CA1", "completed")
    assert log.first_status("CA1", "completed")


def test_calllog_summary_claim_once(tmp_path):
    log = CallLog(tmp_path / "c.sqlite3")
    log.start("CA1", "inbound", "a", "b", "d")
    assert log.claim_summary("CA1") and not log.claim_summary("CA1")


def test_calllog_bump_rejects_other_columns(tmp_path):
    with pytest.raises(ValueError):
        CallLog(tmp_path / "c.sqlite3").bump("CA1", "outcome; DROP TABLE calls")


def test_calllog_outcomes_append(tmp_path):
    log = CallLog(tmp_path / "c.sqlite3")
    log.add_outcome("CA1", "Booked")
    log.add_outcome("CA1", "Handed over", handover=1)
    row = log.get("CA1")
    assert row["outcome"] == "Booked; Handed over" and row["handover"] == 1
    log.add_outcome(None, "ignored")


# settings and disclosure -------------------------------------------------------------------------
def test_defaults_are_safe():
    s = Settings()
    assert s.max_call_seconds == 300 and s.outbound_allowed_prefixes == ("+44",) and not s.trial_mode
    assert s.model == "claude-sonnet-5-5" and s.effort == "low"


@pytest.mark.parametrize("kw", [
    {"team_email": "a@x.com,b@x.com"}, {"team_email": "nobody"}, {"max_call_seconds": 10},
    {"max_call_seconds": 1200}, {"max_turns": 0}, {"daily_call_limit": 0}, {"daily_minutes_budget": 0},
    {"model": "claude-sonnet-latest"}, {"model": "claude-preview-x"}, {"effort": "max"},
    {"speech_model": "googlev2_telephony", "speech_timeout": "auto"}, {"public_base_url": "http://x.test"},
    {"outbound_allowed_prefixes": ("44",)}, {"human_number": "07700900999"},
])
def test_bad_settings_refused(kw):
    with pytest.raises(ValueError):
        Settings(**kw)


def test_from_env(monkeypatch):
    for k, v in {"BUSINESS_NAME": "Acme", "PUBLIC_BASE_URL": "https://a.test/", "TWILIO_ACCOUNT_SID": "AC1",
                 "TWILIO_AUTH_TOKEN": "t", "TWILIO_TRIAL": "1", "OUTBOUND_ALLOWED_PREFIXES": "+44, +353",
                 "MAX_CALL_SECONDS": "120", "RECORD_CALLS": "yes", "HUMAN_NUMBER": "+447700900999"}.items():
        monkeypatch.setenv(k, v)
    s = Settings.from_env()
    assert s.business_name == "Acme" and s.public_base_url == "https://a.test" and s.trial_mode
    assert s.outbound_allowed_prefixes == ("+44", "+353") and s.max_call_seconds == 120 and s.record_calls
    assert s.twilio_auth_token == "t"


def test_disclosure_defaults_and_written_request():
    assert resolve_disclosure("", "", "Cimo", "Acme") == "Hi, this is Cimo, an AI assistant for Acme."
    assert "calling from Acme" in resolve_disclosure("", "", "Cimo", "Acme", outbound=True)
    assert resolve_disclosure("", "2026-11-21 written request", "Cimo", "Acme") == ""
    assert handover_notice("") == DEFAULT_HANDOVER and handover_notice("req") == ""


def test_first_message_order():
    msg = first_message("Hi, AI here.", False)
    assert msg.index("AI here") < msg.index("transcribed") < msg.index("How can I help")


def test_outbound_first_message_states_purpose():
    msg = Settings().outbound_first_message("your check-up")
    assert msg.startswith("Hi, this is Cimo, an AI assistant calling from") and "your check-up" in msg


def test_copying_notifier_keeps_copy_even_if_send_fails(tmp_path):
    from twiliovoice.notify import CopyingNotifier, OutboxNotifier

    class Down:
        def send(self, s, b):
            raise OSError("smtp down")
    n = CopyingNotifier(Down(), OutboxNotifier(tmp_path / "o.jsonl"))
    with pytest.raises(OSError):
        n.send("Call summary", "body")
    assert "Call summary" in (tmp_path / "o.jsonl").read_text()


def test_build_agent_uses_smtp_with_copy(tmp_path):
    from twiliovoice.app import build_agent
    from twiliovoice.brain import FakeBrain
    from twiliovoice.notify import CopyingNotifier

    s = Settings(data_dir=tmp_path, smtp_host="smtp.example.test", team_email="desk@example.test")
    assert isinstance(build_agent(s, brain=FakeBrain()).notifier, CopyingNotifier)

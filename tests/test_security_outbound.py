"""Signed webhooks, admin endpoints, the outbound rules and the kill switch."""
from datetime import datetime, timezone
from urllib.parse import urlencode

import pytest

from twiliovoice import twilio_signature
from twiliovoice.costguard import OutboundRefused, check_outbound
from twiliovoice.mock_twilio import FakeTwilioREST
from twiliovoice.twilio_api import TwilioError

from .conftest import BASE, SID, TOKEN

ADMIN = {"Authorization": "Bearer admin-test"}
FORM = {"AccountSid": SID, "CallSid": "CA" + "9" * 32, "From": "+447700900123", "To": "+447700900000"}


def post(client, path, params, token=TOKEN, url=None):
    items = list(params.items())
    headers = twilio_signature.sign(token, url or BASE + path, items)
    return client.post(path, content=urlencode(items), headers=headers)


def test_unsigned_webhook_refused(env):
    client, agent, tw = env
    r = client.post("/twilio/voice/inbound", content=urlencode(FORM),
                    headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 403


def test_wrong_token_refused(env):
    client, agent, tw = env
    assert post(client, "/twilio/voice/inbound", FORM, token="other-token").status_code == 403


def test_signature_for_another_url_refused(env):
    client, agent, tw = env
    r = post(client, "/twilio/voice/inbound", FORM, url="https://attacker.example/twilio/voice/inbound")
    assert r.status_code == 403


def test_tampered_body_refused(env):
    client, agent, tw = env
    items = list(FORM.items())
    headers = twilio_signature.sign(TOKEN, BASE + "/twilio/voice/inbound", items)
    tampered = dict(FORM, From="+19005550000")
    assert client.post("/twilio/voice/inbound", content=urlencode(tampered), headers=headers).status_code == 403


def test_signed_webhook_from_another_account_refused(env):
    client, agent, tw = env
    assert post(client, "/twilio/voice/inbound", dict(FORM, AccountSid="AC" + "2" * 32)).status_code == 403


def test_signed_webhook_accepted_and_xml(env):
    client, agent, tw = env
    r = post(client, "/twilio/voice/inbound", FORM)
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/xml")


def test_query_string_is_part_of_signature(env):
    client, agent, tw = env
    p = dict(FORM)
    good = post(client, "/twilio/voice/pending?round=1", p, url=BASE + "/twilio/voice/pending?round=1")
    assert good.status_code == 200
    bad = post(client, "/twilio/voice/pending?round=2", p, url=BASE + "/twilio/voice/pending?round=1")
    assert bad.status_code == 403


def test_no_token_configured_is_503(make_env):
    client, agent, tw = make_env(twilio_auth_token=None)
    assert client.post("/twilio/voice/inbound", content="a=b").status_code == 503


def test_oversized_body_refused(env):
    client, agent, tw = env
    assert client.post("/twilio/voice/inbound", content="a=" + "x" * 70000).status_code == 413


def test_allow_unsigned_for_local_dev(make_env):
    client, agent, tw = make_env(allow_unsigned=True)
    r = client.post("/twilio/voice/inbound", content=urlencode(FORM),
                    headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 200


def test_admin_endpoints_need_token(env):
    client, agent, tw = env
    for path in ("/calls", "/leads", "/guard", "/calendar.ics"):
        assert client.get(path).status_code == 401
        assert client.get(path, headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert client.get(path, headers=ADMIN).status_code == 200


def test_health_is_open_and_says_signed(env):
    client, agent, tw = env
    h = client.get("/health").json()
    assert h["ok"] and h["signed_webhooks"] is True and h["model"] == "claude-sonnet-5-5"


def test_calls_endpoint_shows_transcript(env):
    client, agent, tw = env
    tw.run(BASE + "/twilio/voice/inbound", ["Is there parking?", "bye"])
    calls = client.get("/calls", headers=ADMIN).json()
    assert calls[0]["transcript"][1]["text"] == "Is there parking?"


# outbound ---------------------------------------------------------------------------------------
NOON_UTC = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)


def test_outbound_places_call_with_limits(env, rest):
    client, agent, tw = env
    res = agent.place_call("+447700900123", "your check-up tomorrow", now_utc=NOON_UTC)
    c = rest.created[0]
    assert res["call_sid"] == c["sid"] and c["to"] == "+447700900123"
    assert c["url"] == BASE + "/twilio/voice/outbound" and c["status_callback"] == BASE + "/twilio/voice/status"
    assert c["time_limit"] == 300 and c["machine_detection"] is True
    assert agent.log.get(c["sid"])["purpose"] == "your check-up tomorrow"


def test_outbound_greeting_discloses_and_states_purpose(env, rest):
    client, agent, tw = env
    res = agent.place_call("+447700900123", "your check-up tomorrow", now_utc=NOON_UTC)
    call = tw.run(BASE + "/twilio/voice/outbound", ["Yes, fine, bye"], direction="outbound", sid=res["call_sid"])
    assert call.heard[0].startswith("Hi, this is Cimo, an AI assistant calling from Harbour Dental Clinic.")
    assert "I'm calling about your check-up tomorrow." in call.heard[0]


def test_outbound_voicemail_hangs_up_silently(env, rest):
    client, agent, tw = env
    res = agent.place_call("+447700900123", "x", now_utc=NOON_UTC)
    call = tw.run(BASE + "/twilio/voice/outbound", ["hello"], direction="outbound", sid=res["call_sid"],
                  answered_by="machine_start")
    assert call.heard == [] and "Voicemail" in agent.log.get(res["call_sid"])["outcome"]


def test_outbound_endpoint_needs_admin(env):
    client, agent, tw = env
    assert client.post("/outbound", json={"to": "+447700900123"}).status_code == 401


def test_outbound_endpoint_refusal_is_422(env):
    client, agent, tw = env
    r = client.post("/outbound", json={"to": "+15005550006", "purpose": "x"}, headers=ADMIN)
    assert r.status_code == 422 and "OUTBOUND_ALLOWED_PREFIXES" in r.json()["refused"]


def test_outbound_twilio_error_is_502_and_alerted(make_env, alerts):
    client, agent, tw = make_env()
    agent.twilio = FakeTwilioREST(fail_with=TwilioError(400, {"code": 21219, "message": "unverified"}))
    agent.s.calling_windows = {}
    r = client.post("/outbound", json={"to": "+447700900123", "purpose": "x"}, headers=ADMIN)
    assert r.status_code == 502 and r.json()["twilio_error"] == 21219
    assert any("Outbound call failed" in a[0] for a in alerts)


def test_uae_numbers_blocked_by_default():
    with pytest.raises(OutboundRefused, match="UAE"):
        check_outbound("+971501234567", ("+44",), {}, NOON_UTC)


def test_uae_calls_only_9_to_6_dubai_time():
    windows = {"+971": ("Asia/Dubai", "09:00", "18:00")}
    check_outbound("+971501234567", ("+971",), windows, datetime(2026, 11, 24, 6, 0, tzinfo=timezone.utc))  # 10:00
    with pytest.raises(OutboundRefused, match="between 09:00 and 18:00"):
        check_outbound("+971501234567", ("+971",), windows, datetime(2026, 11, 24, 15, 0, tzinfo=timezone.utc))


def test_uk_window_uses_recipient_time():
    windows = {"+44": ("Europe/London", "09:00", "20:00")}
    with pytest.raises(OutboundRefused, match="Europe/London"):
        check_outbound("+447700900123", ("+44",), windows, datetime(2026, 11, 24, 7, 0, tzinfo=timezone.utc))


@pytest.mark.parametrize("bad", ["07700900123", "+44abc", "+1", "", "+44 7700 900123"])
def test_outbound_number_must_be_e164(bad):
    with pytest.raises(OutboundRefused):
        check_outbound(bad, ("+44",), {}, NOON_UTC)


def test_outbound_refused_when_paused(make_env):
    client, agent, tw = make_env(paused=True)
    with pytest.raises(OutboundRefused, match="kill switch"):
        agent.place_call("+447700900123", "x", now_utc=NOON_UTC)


def test_outbound_refused_without_from_number(make_env):
    client, agent, tw = make_env(twilio_from_number="")
    with pytest.raises(OutboundRefused, match="TWILIO_FROM_NUMBER"):
        agent.place_call("+447700900123", "x", now_utc=NOON_UTC)


def test_outbound_refused_without_twilio(make_env):
    client, agent, tw = make_env()
    agent.twilio = None
    with pytest.raises(OutboundRefused, match="not configured"):
        agent.place_call("+447700900123", "x", now_utc=NOON_UTC)


def test_outbound_respects_daily_call_limit(make_env):
    client, agent, tw = make_env(daily_call_limit=1)
    agent.place_call("+447700900123", "x", now_utc=NOON_UTC)
    with pytest.raises(OutboundRefused, match="call limit"):
        agent.place_call("+447700900123", "x", now_utc=NOON_UTC)


def test_pause_endpoint_hangs_up_live_calls(env, rest):
    client, agent, tw = env
    res = agent.place_call("+447700900123", "x", now_utc=NOON_UTC)
    out = client.post("/guard/pause", headers=ADMIN).json()
    assert out["paused"] and res["call_sid"] in out["hung_up"] and rest.hung_up == [res["call_sid"]]
    assert client.get("/guard", headers=ADMIN).json()["allowed"] is False
    assert client.post("/guard/resume", headers=ADMIN).json() == {"paused": False}


def test_paused_mid_call_wraps_up(env):
    client, agent, tw = env

    def pause(c):
        agent.guard.pause()
    call = tw.run(BASE + "/twilio/voice/inbound", ["Is there parking?", "more"], on_step=pause)
    assert call.hung_up and "wrap up" in call.heard[-1]


def test_status_callback_unsigned_refused(env):
    client, agent, tw = env
    call = tw.run(BASE + "/twilio/voice/inbound", ["bye"])
    r = tw.send_status(call, "+447700900123", "+447700900000", "busy", 0, sign=False)
    assert r.status_code == 403


def test_status_for_unknown_call_creates_row(env):
    client, agent, tw = env
    r = post(client, "/twilio/voice/status", dict(FORM, CallSid="CA" + "7" * 32, CallStatus="no-answer",
                                                 CallDuration="0"))
    assert r.status_code == 200 and agent.log.get("CA" + "7" * 32)["status"] == "no-answer"


def test_status_failure_lets_twilio_retry(env, monkeypatch, alerts):
    client, agent, tw = env
    sid = "CA" + "8" * 32
    monkeypatch.setattr(agent, "send_summary", lambda s: (_ for _ in ()).throw(RuntimeError("smtp down")))
    p = dict(FORM, CallSid=sid, CallStatus="completed", CallDuration="5")
    assert post(client, "/twilio/voice/status", p).status_code == 500
    assert any("status callback failed" in a[0] for a in alerts)
    monkeypatch.undo()
    assert post(client, "/twilio/voice/status", p).status_code == 200
    assert agent.log.get(sid)["summary_emailed"] == 1


def test_pending_without_task_reprompts(env):
    client, agent, tw = env
    r = post(client, "/twilio/voice/pending?round=1", FORM, url=BASE + "/twilio/voice/pending?round=1")
    assert "<Gather" in r.text


def test_reconcile_finishes_calls_without_status_callback(env, rest, notifier):
    client, agent, tw = env
    sid = "CA" + "5" * 32
    post(client, "/twilio/voice/inbound", dict(FORM, CallSid=sid))          # trial inbound: no status URL
    old = "2026-01-01T00:00:00+00:00"
    agent.log._db.execute("UPDATE calls SET started_utc=? WHERE call_sid=?", (old, sid))
    agent.log._db.commit()
    rest.calls[sid] = {"sid": sid, "status": "completed", "duration": "42"}
    out = client.post("/reconcile", headers=ADMIN).json()["finished"]
    assert out == [{"call_sid": sid, "status": "completed", "duration": "42"}]
    assert agent.log.get(sid)["duration_seconds"] == 42
    assert any(s.startswith("Call summary") for s in notifier.subjects())
    assert client.post("/reconcile", headers=ADMIN).json()["finished"] == []   # already final


def test_reconcile_skips_recent_and_live_calls(env, rest):
    client, agent, tw = env
    sid = "CA" + "6" * 32
    post(client, "/twilio/voice/inbound", dict(FORM, CallSid=sid))
    assert client.post("/reconcile", headers=ADMIN).json()["finished"] == []       # too recent
    assert client.post("/reconcile?older_than=0", headers=ADMIN).json()["finished"] == []  # still in progress


def test_reconcile_needs_admin(env):
    client, agent, tw = env
    assert client.post("/reconcile").status_code == 401

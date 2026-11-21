from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from twiliovoice.app import build_agent, create_app
from twiliovoice.brain import FakeBrain
from twiliovoice.config import Settings
from twiliovoice.mock_twilio import FakeTwilioREST, MockTwilio

TOKEN = "test-auth-token-not-real"
SID = "AC" + "1" * 32
BASE = "https://voice.example.test"


class ListNotifier:
    def __init__(self):
        self.sent = []

    def send(self, subject, body):
        self.sent.append((subject, body))

    def subjects(self):
        return [s for s, _ in self.sent]


class NullHeartbeat:
    def __init__(self):
        self.records = []

    def record(self, n):
        self.records.append(n)
        return True


def make_settings(tmp_path, **kw):
    base = dict(data_dir=tmp_path, public_base_url=BASE, twilio_account_sid=SID, twilio_auth_token=TOKEN,
                twilio_from_number="+447700900000", admin_token="admin-test", reply_wait_seconds=2.0)
    base.update(kw)
    return Settings(**base)


@pytest.fixture
def notifier():
    return ListNotifier()


@pytest.fixture
def alerts():
    return []


@pytest.fixture
def rest():
    return FakeTwilioREST()


@pytest.fixture
def make_env(tmp_path, notifier, alerts, rest):
    """Factory: (settings overrides, brain) -> (client, agent, mock twilio). Use inside `with`."""
    made = []

    def _make(brain=None, **kw):
        s = make_settings(tmp_path, **kw)
        hb = NullHeartbeat()
        agent = build_agent(s, brain=brain or FakeBrain(), notifier=notifier, twilio=rest,
                            alert=lambda subj, detail: alerts.append((subj, detail)), heartbeat=hb)
        app = create_app(s, agent=agent)
        client = TestClient(app)
        client.__enter__()
        made.append(client)
        tw = MockTwilio(client, TOKEN, BASE, account_sid=SID)
        return client, agent, tw

    yield _make
    for c in made:
        c.__exit__(None, None, None)


@pytest.fixture
def env(make_env):
    return make_env()


# unit fixtures (shared with the ElevenLabs build's tests for the same calendar/tools code)
from twiliovoice.calendar.local import LocalCalendar  # noqa: E402
from twiliovoice.calllog import CallLog  # noqa: E402
from twiliovoice.config import DEFAULT_HOURS  # noqa: E402
from twiliovoice.costguard import CostGuard  # noqa: E402
from twiliovoice.scheduling import Scheduler  # noqa: E402
from twiliovoice.tools import VoiceAgentTools  # noqa: E402

from .unit_fixtures import FIXED_NOW, TZ  # noqa: E402


class Alerts:
    def __init__(self):
        self.sent = []

    def __call__(self, subject, detail):
        self.sent.append((subject, detail))


@pytest.fixture
def unit_alerts():
    return Alerts()


@pytest.fixture
def calendar(tmp_path):
    return LocalCalendar(tmp_path / "cal.sqlite3")


@pytest.fixture
def calllog(tmp_path):
    return CallLog(tmp_path / "calls.sqlite3")


@pytest.fixture
def guard(calllog, unit_alerts):
    return CostGuard(calllog, TZ, daily_minutes_budget=60, daily_call_limit=5, alert=unit_alerts,
                     now=lambda: FIXED_NOW)


@pytest.fixture
def tools(calendar, notifier, calllog, guard, unit_alerts):
    sched = Scheduler(calendar, TZ, DEFAULT_HOURS, 30, 60)
    sched.now = lambda: FIXED_NOW
    return VoiceAgentTools(sched, calendar, notifier, calllog, "Test Clinic", guard, on_error=unit_alerts)

"""A small client for Twilio's Programmable Voice REST API (version 2010-04-01).

Create a call: POST /Accounts/{AccountSid}/Calls.json, form-encoded, HTTP Basic auth
(Account SID : Auth Token). The answer is the Call resource ({"sid": "CA...", "status": "queued"}).
An error is {"code": 21219, "message": "...", "more_info": "...", "status": 400}.

Retries are narrow on purpose: Twilio's Calls API takes no idempotency key, so a request that may
have reached Twilio is never sent again (it could ring the person twice). Only a 429 (refused,
nothing was dialled) and a connection that never opened are retried.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Dict, List, Optional
from urllib.parse import urlencode

import httpx

from .config import TWILIO_API_BASE

# Codes worth knowing by name (Twilio's error dictionary).
KNOWN_ERRORS = {
    20003: "Authentication failed",
    20429: "Too many requests",
    13224: "Invalid phone number in <Dial>",
    21205: "Url is not a valid URL",
    21210: "'From' is not a verified number or a Twilio number on this account",
    21211: "Invalid 'To' phone number",
    21212: "Invalid 'From' phone number",
    21214: "'To' number cannot be reached",
    21215: "Geo permissions: calls to this country are not enabled",
    21219: "Trial accounts can only call verified numbers",
    21220: "Call is not in progress, cannot be changed",
    32011: "Error on the trial account (template or verb not allowed)",
}

FINAL = {"completed", "busy", "no-answer", "failed", "canceled"}


class TwilioError(Exception):
    def __init__(self, status: int, body: Optional[dict]):
        body = body or {}
        self.status = status
        self.code = body.get("code")
        self.title = KNOWN_ERRORS.get(self.code, "") if isinstance(self.code, int) else ""
        self.detail = body.get("message", "")
        self.more_info = body.get("more_info", "")
        super().__init__(f"Twilio HTTP {status} code {self.code}: {self.title or self.detail}")


@dataclass
class CallResult:
    sid: str
    status: str
    raw: dict


class TwilioVoiceClient:
    def __init__(self, account_sid: str, auth_token: str, base_url: str = TWILIO_API_BASE,
                 http: Optional[httpx.Client] = None, max_retries: int = 2, backoff: float = 1.0,
                 sleep=time.sleep):
        if not account_sid or not auth_token:
            raise ValueError("TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN must both be set")
        self.account_sid = account_sid
        self.base_url = base_url.rstrip("/")
        self.http = http or httpx.Client(timeout=15)
        self.auth = (account_sid, auth_token)
        self.max_retries = max_retries
        self.backoff = backoff
        self.sleep = sleep

    def _request(self, method: str, path: str, form=None) -> dict:
        url = f"{self.base_url}/Accounts/{self.account_sid}{path}"
        for attempt in range(self.max_retries + 1):
            try:
                # Encoded by hand: StatusCallbackEvent repeats, and httpx's data= takes no repeated keys.
                body = urlencode(list(form.items()) if isinstance(form, dict) else form) if form else None
                headers = {"Accept": "application/json"}
                if body is not None:
                    headers["Content-Type"] = "application/x-www-form-urlencoded"
                r = self.http.request(method, url, content=body, auth=self.auth, headers=headers)
            except httpx.ConnectError:
                if attempt == self.max_retries:
                    raise
                self.sleep(self.backoff * 2 ** attempt)
                continue
            if r.status_code < 300:
                return r.json() if r.content else {}
            if r.status_code == 429 and attempt < self.max_retries:
                wait = r.headers.get("Retry-After")
                self.sleep(float(wait) if wait and wait.isdigit() else self.backoff * 2 ** attempt)
                continue
            try:
                body = r.json()
            except ValueError:
                body = {"message": r.text[:200]}
            raise TwilioError(r.status_code, body)
        raise RuntimeError("unreachable")

    def create_call(self, to: str, from_number: str, url: str, status_callback: str,
                    time_limit: int, ring_timeout: int = 30, record: bool = False,
                    machine_detection: bool = False) -> CallResult:
        """Place an outbound call. Twilio fetches TwiML from `url` once the person answers."""
        form: List[tuple] = [("To", to), ("From", from_number), ("Url", url), ("Method", "POST"),
                             ("StatusCallback", status_callback), ("StatusCallbackMethod", "POST"),
                             ("TimeLimit", str(time_limit)), ("Timeout", str(ring_timeout))]
        # One parameter per event (Twilio's rule for StatusCallbackEvent).
        for event in ("initiated", "ringing", "answered", "completed"):
            form.append(("StatusCallbackEvent", event))
        if record:
            form.append(("Record", "true"))
        if machine_detection:
            form.append(("MachineDetection", "Enable"))
        data = self._request("POST", "/Calls.json", form)
        return CallResult(sid=data["sid"], status=data.get("status", "queued"), raw=data)

    def hangup(self, call_sid: str) -> dict:
        """End a live call (the kill switch and the max-length guard use this)."""
        return self._request("POST", f"/Calls/{call_sid}.json", {"Status": "completed"})

    def get_call(self, call_sid: str) -> dict:
        return self._request("GET", f"/Calls/{call_sid}.json")

    def account(self) -> Dict[str, str]:
        """Account type and status only (never pass the full resource on: it contains the auth token)."""
        data = self._request("GET", ".json")
        return {"type": data.get("type", ""), "status": data.get("status", "")}

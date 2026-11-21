"""AI disclosure (EU AI Act Article 50, playbook rule B18) and the recording notice, spoken first.

The AI line comes from AI_DISCLOSURE. Blanking it does NOT switch it off: that only happens when
AI_DISCLOSURE_REMOVED_BY_WRITTEN_REQUEST holds a reference to the client's written request
(date and who asked). Without that reference an empty value falls back to the default.

The recording notice follows RECORD_CALLS: if Twilio records the audio the caller is told so;
otherwise the caller is told the call is transcribed (speech recognition turns every answer
into text, which is kept in the call log). It can't be blanked.
"""
from __future__ import annotations

from typing import Optional

DEFAULT_DISCLOSURE = "Hi, this is {name}, an AI assistant for {business}."
DEFAULT_OUTBOUND_DISCLOSURE = "Hi, this is {name}, an AI assistant calling from {business}."
DEFAULT_HANDOVER = "I'm an AI assistant, so I'm passing you to a person on the team."
RECORDING_NOTICE = "This call is recorded and transcribed so the team can follow up."
TRANSCRIPT_NOTICE = "This call is transcribed so the team can follow up."
DEFAULT_QUESTION = "How can I help today?"


def _fill(text: str, name: str, business: str) -> str:
    return text.replace("{name}", name).replace("{business}", business).strip()


def resolve_disclosure(text: Optional[str], removed_by_written_request: Optional[str],
                       name: str, business: str, outbound: bool = False) -> str:
    """The opening disclosure line. Empty only when a written-request reference is recorded."""
    if (removed_by_written_request or "").strip():
        return _fill(text or "", name, business)
    default = DEFAULT_OUTBOUND_DISCLOSURE if outbound else DEFAULT_DISCLOSURE
    return _fill((text or "").strip() or default, name, business)


def handover_notice(removed_by_written_request: Optional[str]) -> str:
    """What the caller hears when they are passed to a person."""
    return "" if (removed_by_written_request or "").strip() else DEFAULT_HANDOVER


def recording_notice(record_calls: bool) -> str:
    return RECORDING_NOTICE if record_calls else TRANSCRIPT_NOTICE


def first_message(disclosure: str, record_calls: bool, question: str = DEFAULT_QUESTION) -> str:
    """Disclosure, then the recording notice, then the question: the AI line always comes first."""
    parts = [disclosure.strip(), recording_notice(record_calls), question.strip()]
    return " ".join(p for p in parts if p)

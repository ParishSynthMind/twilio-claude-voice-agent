"""TwiML (Twilio's XML call instructions), built with an XML library so nothing a caller or the model
says can break out of a <Say> or inject a verb.

Only the verbs this agent needs: <Say>, <Gather input="speech">, <Pause>, <Redirect>, <Dial>, <Hangup>.
On a Twilio trial account <Dial> to a phone number is replaced by a spoken "not available on trial
accounts" message (Twilio docs, trials page, read 21 Nov 2026), so trial mode never emits it.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import List, Optional

MAX_SAY_CHARS = 600   # one spoken turn; long answers are cut at a sentence end
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def speakable(text: str, limit: int = MAX_SAY_CHARS) -> str:
    """Plain words for text to speech: no markdown, no control characters, no links, capped."""
    t = _CONTROL.sub(" ", text or "")
    t = re.sub(r"https?://\S+|www\.\S+", "", t)
    t = t.replace("**", "").replace("__", "").replace("`", "").replace("#", "")
    t = re.sub(r"^\s*[-*•]\s+", "", t, flags=re.M)
    t = " ".join(t.split())
    if len(t) <= limit:
        return t
    cut = t[:limit]
    end = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
    return (cut[: end + 1] if end > limit // 3 else cut.rsplit(" ", 1)[0] + ".").strip()


@dataclass
class Voice:
    voice: str
    language: str


@dataclass
class GatherSpec:
    action: str                  # absolute URL (needed when a call was created with inline TwiML)
    language: str
    speech_timeout: str = "auto"
    speech_model: str = "default"
    timeout: int = 6
    hints: Optional[List[str]] = None


class Response:
    def __init__(self, voice: Voice):
        self.root = ET.Element("Response")
        self.voice = voice

    def _say(self, parent: ET.Element, text: str) -> None:
        text = speakable(text)
        if text:
            el = ET.SubElement(parent, "Say", voice=self.voice.voice, language=self.voice.language)
            el.text = text

    def say(self, text: str) -> "Response":
        self._say(self.root, text)
        return self

    def gather(self, text: str, spec: GatherSpec) -> "Response":
        attrs = {"input": "speech", "action": spec.action, "method": "POST", "language": spec.language,
                 "speechTimeout": str(spec.speech_timeout), "timeout": str(spec.timeout),
                 "actionOnEmptyResult": "true"}
        if spec.speech_model and spec.speech_model != "default":
            attrs["speechModel"] = spec.speech_model
        if spec.hints:
            attrs["hints"] = ", ".join(h.replace(",", " ")[:100] for h in spec.hints[:50])
        g = ET.SubElement(self.root, "Gather", **attrs)
        self._say(g, text)
        return self

    def pause(self, seconds: int = 1) -> "Response":
        ET.SubElement(self.root, "Pause", length=str(max(1, min(seconds, 10))))
        return self

    def redirect(self, url: str) -> "Response":
        el = ET.SubElement(self.root, "Redirect", method="POST")
        el.text = url
        return self

    def dial(self, number: str, action: str, timeout: int = 20, time_limit: int = 600) -> "Response":
        el = ET.SubElement(self.root, "Dial", action=action, method="POST", timeout=str(timeout),
                           timeLimit=str(time_limit))
        num = ET.SubElement(el, "Number")
        num.text = number
        return self

    def hangup(self) -> "Response":
        ET.SubElement(self.root, "Hangup")
        return self

    def xml(self) -> str:
        return '<?xml version="1.0" encoding="UTF-8"?>' + ET.tostring(self.root, encoding="unicode")


def parse(xml_text: str) -> ET.Element:
    """For tests and the mock: parse TwiML back into elements."""
    return ET.fromstring(xml_text.split("?>", 1)[1] if xml_text.startswith("<?xml") else xml_text)

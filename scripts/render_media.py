"""Render the README and portfolio images headless (needs playwright + chromium).

    python scripts/render_media.py --calls examples/live-run/calls.json --date "21 Nov 2026"

media/thumbnail-4x3.png   1200x900 portfolio card
media/live-call-<n>.png   one image per call: what the caller heard and said, tool calls, latency
media/how-it-works.png    the call flow
"""
import argparse
import html
import json
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
MEDIA = ROOT / "media"
STYLE = """<style>
 body{margin:0;background:#0f1115;font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif;color:#e6edf3}
 .wrap{width:1080px;padding:36px 40px;box-sizing:border-box}
 h2{margin:0 0 6px;font-size:30px} .sub{color:#9aa4b2;font-size:18px;margin-bottom:22px}
 .row{display:flex;margin:8px 0} .row.c{justify-content:flex-start} .row.a{justify-content:flex-end}
 .b{max-width:760px;border-radius:18px;padding:12px 18px;font-size:19px;line-height:1.38}
 .c .b{background:#2c313c} .a .b{background:#1f6f50}
 .who{font-size:13px;color:#9aa4b2;margin:0 6px 2px} .tool{font-size:15px;color:#ffd479;margin:2px 10px 8px;text-align:right}
 .lat{font-size:13px;color:#7fe3b5;margin-left:8px}
 .foot{margin-top:18px;color:#9aa4b2;font-size:15px}
 .thumb{width:1200px;height:900px;box-sizing:border-box;padding:70px 80px;background:
   radial-gradient(circle at 85% 12%,#f22f46 0,#f22f4600 40%),linear-gradient(135deg,#0f1115 0,#1b1f2a 100%);
   display:flex;flex-direction:column;justify-content:space-between}
 .thumb h1{font-size:66px;line-height:1.06;margin:0} .thumb .k{font-size:28px;color:#c9d1d9;margin-top:22px;line-height:1.4}
 .pill{display:inline-block;border:2px solid #3ecf8e;color:#7fe3b5;border-radius:40px;padding:10px 22px;margin:0 12px 12px 0;
   font-size:24px} .pill.red{border-color:#f22f46;color:#ff8a98}
 .bub{background:#2c313c;border-radius:22px;padding:16px 22px;font-size:26px;max-width:700px;margin:10px 0}
 .bub.o{background:#1f6f50;margin-left:auto}
 .flow{display:flex;align-items:center;gap:14px;margin:26px 0} .box{border:2px solid #3b4252;border-radius:14px;padding:16px;
   font-size:18px;text-align:center;min-width:150px;background:#161a22} .arr{font-size:28px;color:#9aa4b2}
 .box.t{border-color:#f22f46} .box.s{border-color:#3ecf8e} .box.l{border-color:#d4a373}
</style>"""


def page(body, cls="wrap"):
    return f"<html><head><meta charset='utf-8'>{STYLE}</head><body><div class='{cls}'>{body}</div></body></html>"


def shoot(pw, markup, name, sel=".wrap", scale=2):
    b = pw.chromium.launch()
    p = b.new_page(viewport={"width": 1200, "height": 900}, device_scale_factor=scale)
    p.set_content(markup)
    p.locator(sel).screenshot(path=str(MEDIA / name))
    b.close()


def thumb(date, q, a):
    pills = "".join(f"<span class='pill{c}'>{t}</span>" for t, c in [
        ("Twilio Voice, live", " red"), ("Claude + tools", ""), ("AI disclosure first", ""),
        ("signed webhooks", ""), ("booking + hand-over", ""), ("cost guard", "")])
    return page("<div><h1>An AI phone agent<br>on Twilio, with Claude</h1>"
                "<div class='k'>Real phone calls answered by Claude: questions, diary, urgent hand-over.<br>"
                f"Live-tested on a Twilio trial, {html.escape(date)}.</div></div>"
                f"<div><div class='bub'>{html.escape(q)}</div><div class='bub o'>{html.escape(a)}</div></div>"
                f"<div>{pills}</div>", "thumb")


def call_image(call, title, sub):
    rows = []
    tools = {}
    for e in call.get("tool_events", []):
        tools.setdefault(e["at_utc"][:19], []).append(e["tool"])
    for t in call["transcript"]:
        side = "c" if t["speaker"] == "caller" else "a"
        who = "Caller (Twilio speech recognition)" if side == "c" else "Agent (Claude, spoken by Twilio)"
        lat = f"<span class='lat'>{t['latency_ms'] / 1000:.1f} s</span>" if t.get("latency_ms") else ""
        conf = f" &middot; confidence {t['confidence']:.2f}" if t.get("confidence") else ""
        rows.append(f"<div class='row {side}'><div><div class='who'>{who}{conf}{lat}</div>"
                    f"<div class='b'>{html.escape(t['text'])}</div></div></div>")
    used = [e["tool"] for e in call.get("tool_events", [])]
    foot = (f"Tools called: {', '.join(used) or 'none'} &middot; outcome: {html.escape(call.get('outcome') or '-')}"
            f" &middot; {call.get('duration_seconds') or '?'} s &middot; {html.escape(call.get('direction') or '')}")
    return page(f"<h2>{html.escape(title)}</h2><div class='sub'>{html.escape(sub)}</div>{''.join(rows)}"
                f"<div class='foot'>{foot}</div>")


def flow():
    boxes = ["<div class='box'>Caller's phone</div>", "<div class='arr'>&harr;</div>",
             "<div class='box t'>Twilio Voice<br><small>speech recognition<br>text to speech</small></div>",
             "<div class='arr'>&harr;</div>",
             "<div class='box s'>This service<br><small>signed TwiML webhooks<br>cost guard, call log</small></div>",
             "<div class='arr'>&harr;</div>", "<div class='box l'>Claude<br><small>knowledge base<br>5 tools</small></div>"]
    return page("<h2>How a call works</h2><div class='sub'>Every turn: the caller speaks, Twilio posts the words, "
                "Claude answers (and books, captures a lead or hands over), Twilio speaks the answer.</div>"
                f"<div class='flow'>{''.join(boxes)}</div><div class='sub'>Calendar (SQLite + .ics or Google) "
                "&middot; team email: summary after every call, URGENT hand-overs &middot; status callbacks "
                "bring the duration for the daily minutes budget</div>")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--calls", default="examples/live-run/calls.json")
    ap.add_argument("--date", default="21 Nov 2026")
    ap.add_argument("--titles", default="", help="json list of [title, sub] per call, oldest first")
    a = ap.parse_args()
    MEDIA.mkdir(exist_ok=True)
    calls = list(reversed(json.loads(Path(a.calls).read_text())))
    titles = json.loads(a.titles) if a.titles else []
    with sync_playwright() as pw:
        for i, c in enumerate(calls, 1):
            t, s = titles[i - 1] if i - 1 < len(titles) else (f"Live call {i}", a.date)
            shoot(pw, call_image(c, t, s), f"live-call-{i}.png")
        q = next((x["text"] for c in calls for x in c["transcript"] if x["speaker"] == "caller"), "Is there parking?")
        ans = next((x["text"] for c in calls for j, x in enumerate(c["transcript"])
                    if x["speaker"] == "agent" and j > 0), "Yes, free parking behind the building.")
        shoot(pw, thumb(a.date, q, ans.split(". ")[0].rstrip(".") + "."), "thumbnail-4x3.png", ".thumb", scale=1)
        shoot(pw, flow(), "how-it-works.png")
    print("rendered", len(calls), "call images + thumbnail + flow ->", MEDIA)


if __name__ == "__main__":
    main()

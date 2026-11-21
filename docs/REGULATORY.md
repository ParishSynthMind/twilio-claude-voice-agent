# Calling rules this build is set up for (UK and UAE)

**Not legal advice.** These are the rules the defaults were built around, from public sources read on 21 November 2026. A business that uses this agent is responsible for its own calls. Check with a lawyer before you make marketing calls, or before you call a new country.

## What the build does by default

| Rule | Where it is enforced |
|---|---|
| The caller hears that they are talking to an AI, and that the call is transcribed or recorded, in the **first sentence** | `twiliovoice/disclosure.py`. Blanking `AI_DISCLOSURE` falls back to the default line. Only `AI_DISCLOSURE_REMOVED_BY_WRITTEN_REQUEST` (date and who asked) removes it |
| Outbound calls go only to countries listed in `OUTBOUND_ALLOWED_PREFIXES` (default `+44`) | `costguard.check_outbound` |
| Outbound calls are placed only inside the **recipient's** calling hours (UK default 09:00 to 20:00 London time) | `config.DEFAULT_CALLING_WINDOWS` |
| **UAE numbers (+971) are refused** unless the business adds `+971` on purpose. Once added, calls are only placed 09:00 to 18:00 Dubai time | `costguard.check_outbound` |
| The person called always gets the business name and why you are calling, before anything else is asked | `Settings.outbound_first_message` |
| A voicemail (answering machine detected) is hung up on without leaving a message | `service.outbound_answered` |
| Every call has a length cap, every day has a call cap and a minutes budget, and a kill switch hangs up live calls | `costguard.py`, `/guard/pause` |

## United Kingdom

- **AI and recording notice.** UK GDPR and the ICO's guidance expect callers to be told when a call is recorded and why. The EU AI Act's Article 50 (in force from 2 August 2026) covers businesses serving EU callers. This build does both in its first sentence.
- **Marketing calls.** Under PECR, live marketing calls must not go to numbers on the Telephone Preference Service (TPS) unless that person has agreed to calls from you. **Automated** marketing calls (a recorded or AI voice) need the person's prior consent, whatever the TPS says. This build is meant for **service calls** to existing customers: reminders, call-backs and confirmations. It is not a sales dialler.
- **Ofcom's persistent misuse policy** is aimed at silent and abandoned calls. Show a caller ID that can be called back (this build calls from the business's Twilio number). Don't ring the same person again and again. If you use answering machine detection at scale, keep within Ofcom's abandoned-call limits.

## United Arab Emirates

- **Cabinet Resolutions 56 and 57 of 2024**, on telemarketing and its penalties, have been in force since 27 August 2024 (Clyde & Co, July 2024; official text on uaelegislation.gov.ae). They apply to companies licensed in the UAE, free-zone companies included. In short:
  - Marketing calls are allowed **only between 09:00 and 18:00**.
  - They must come from **numbers registered to the company**. Personal numbers are banned.
  - Never call a number on the TDRA's **Do Not Call Register (DNCR)**. Company fines reported for that are AED 50,000, then 75,000, then 150,000.
  - Using unregistered numbers is fined separately.
- **What the build does about it.** UAE numbers are off by default. Turning them on enforces the 09:00 to 18:00 Dubai window. The **DNCR check and the number registration are the business's job**, and they must be done before `+971` is added. This build has no DNCR lookup.
- A Twilio number is not a UAE-registered company number. A UAE business would need a local carrier or a provider with UAE-registered numbers for outbound marketing calls.

## Twilio trial accounts (what was seen on the live test)
See [TWILIO-FACTS.md](TWILIO-FACTS.md).

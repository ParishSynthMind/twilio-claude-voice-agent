# Twilio Voice facts this build relies on

Each fact says where it comes from: **docs** (a Twilio page, with the date it was read) or **live** (seen on the live test).

## Webhooks and TwiML
- Twilio signs every webhook with `X-Twilio-Signature`. The signature is HMAC-SHA1, keyed with the account's auth token, over the exact URL Twilio called (query string included) followed by every POST parameter sorted by name. The URL is used exactly as received: decoding or re-encoding it breaks the check. *(docs: webhooks-security, read 21 Nov 2026)*
- `<Gather input="speech">` posts `SpeechResult` and `Confidence` (0.0 to 1.0) to `action`. Confidence is not guaranteed to be present. *(docs: TwiML Gather, modified 2026-10-08)*
  - With `actionOnEmptyResult="true"`, silence posts to `action` too.
  - `speechTimeout="auto"` stops at a pause.
  - A specific `speechModel` needs a whole number for `speechTimeout`, never `auto`. The config refuses that mix.
  - Speech is collected for at most 60 seconds.
- Only `<Say>`, `<Play>` and `<Pause>` may sit inside `<Gather>`. *(docs: TwiML Gather)*
- If a call is created with inline `Twiml`, `action` URLs must be absolute. This build always uses absolute URLs. *(docs)*
- `StatusCallbackEvent` is sent once per event (`initiated`, `ringing`, `answered`, `completed`). The status callback's `CallDuration` is the call length in seconds. *(docs: Call resource, modified 2026-06-22)*
- Twilio has no idempotency key on Create Call. This client never retries a request that may have reached Twilio, only a 429 or a connection that never opened. *(docs + design)*

## Trial accounts (docs: Try out Voice, modified 2026-09-25; trials page, modified 2026-11-13)
- Calls go only to and from **verified** numbers, and only within the sign-up country.
- Limits: 10 minutes per call, 75 voice minutes in total, 5 concurrent calls, and a 5-second TwiML fetch timeout. The 5-second timeout is why this build has the "one moment" path.
- `<Dial>` to a number, `<Record>`, `<Stream>` and `<ConversationRelay>` are replaced on trial by a spoken "not available on trial accounts". `<Gather>` speech is fully supported. `TWILIO_TRIAL=1` makes this build never send `<Dial>`.
- The trial number is a shared Twilio number. It is not listed under `IncomingPhoneNumbers` in the REST API. Inbound calls are pointed at a server in the console: **Voice > Try out Voice > Receive a call > Custom > Webhook URL**. That screen has no status-callback field, so inbound calls on a trial get no final status callback. `POST /reconcile` finishes them from the REST API. *(live, console read 21 Nov 2026)*
- Outbound calls from the API with your own `Url` are allowed on trial: the console's **Start a call > Custom** asks for a "Custom TwiML URL". *(live, console read 21 Nov 2026)*

<!-- LIVE-FACTS -->

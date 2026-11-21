#!/bin/bash
# Live run on this Mac: keys come from the macOS Keychain into the environment, never a file.
# Usage: PUBLIC_BASE_URL=https://<tunnel> scripts/run_live.sh
set -euo pipefail
cd "$(dirname "$0")/.."
: "${PUBLIC_BASE_URL:?set PUBLIC_BASE_URL to the https tunnel address}"
export TWILIO_SID_KEYCHAIN="${TWILIO_SID_KEYCHAIN:-neo-twilio-trial-sid}"
export TWILIO_TOKEN_KEYCHAIN="${TWILIO_TOKEN_KEYCHAIN:-neo-twilio-trial-token}"
export ANTHROPIC_API_KEY="$(security find-generic-password -s "${ANTHROPIC_KEYCHAIN:-anthropic-api-key}" -w)"
export ADMIN_TOKEN="${ADMIN_TOKEN:-$(security find-generic-password -s twilio-voice-admin-token -w 2>/dev/null || true)}"
export DATA_DIR="${DATA_DIR:-var/live}"
export TWILIO_TRIAL="${TWILIO_TRIAL:-1}"
export MAX_CALL_SECONDS="${MAX_CALL_SECONDS:-180}"
export DAILY_MINUTES_BUDGET="${DAILY_MINUTES_BUDGET:-10}"
export DAILY_CALL_LIMIT="${DAILY_CALL_LIMIT:-6}"
exec .venv/bin/uvicorn twiliovoice.main:app --host 127.0.0.1 --port "${PORT:-8000}" --workers 1

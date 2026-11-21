"""Where keys live: environment variables first, then the macOS Keychain. Never a file in the repo."""
from __future__ import annotations

import os
import subprocess
import sys
from typing import Optional


def get(env_name: str, keychain_service: Optional[str] = None) -> Optional[str]:
    value = os.environ.get(env_name)
    if value:
        return value
    if keychain_service and sys.platform == "darwin":
        out = subprocess.run(["security", "find-generic-password", "-s", keychain_service, "-w"],
                             capture_output=True, text=True)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    return None


def put(keychain_service: str, value: str, account: str = "voiceagent") -> bool:
    """Store a generated secret in the Keychain (macOS). False elsewhere: the caller tells the user to
    put it in their own secret store instead. The value is never printed."""
    if sys.platform != "darwin":
        return False
    subprocess.run(["security", "add-generic-password", "-U", "-s", keychain_service, "-a", account, "-w", value],
                   check=True, capture_output=True)
    return True

#!/usr/bin/env python3
"""Securely save the Canvas token without echoing it or putting it in shell history."""

import getpass
import os
import subprocess
import sys
from pathlib import Path


ENV_PATH = Path(__file__).resolve().parent / ".env"
BASE_URL = "https://utexas.instructure.com"


def get_token() -> str:
    if "--gui" not in sys.argv:
        return getpass.getpass("Paste your Canvas token here, then press Return (input is hidden): ").strip()
    script = (
        'text returned of (display dialog "Paste your Canvas access token:" '
        'default answer "" with hidden answer buttons {"Cancel", "Save"} '
        'default button "Save" with title "Canvas Agent Setup")'
    )
    result = subprocess.run(
        ["/usr/bin/osascript", "-e", script],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def main() -> int:
    token = get_token()
    if not token:
        print("No token entered; .env was not changed.")
        return 1

    temporary = ENV_PATH.with_suffix(".env.tmp")
    temporary.write_text(
        "# Canvas credentials - never share or commit this file.\n"
        f"CANVAS_BASE_URL={BASE_URL}\n"
        f"CANVAS_TOKEN={token}\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    temporary.replace(ENV_PATH)
    os.chmod(ENV_PATH, 0o600)
    print("Canvas configuration saved securely. The token was not displayed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

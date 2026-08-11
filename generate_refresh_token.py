#!/usr/bin/env python3
"""generate_refresh_token.py — one-time OAuth setup for gads-scan.

Run it once, paste your OAuth client id and secret, approve the consent screen in
the browser it opens, and it prints the refresh token to put in google-ads.yaml.

    python generate_refresh_token.py

Nothing is stored or sent anywhere: the token is printed to your terminal and
that is the end of it.
"""
from __future__ import annotations

import sys

SCOPE = "https://www.googleapis.com/auth/adwords"


def main() -> int:
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("ERROR: install the dependencies first:\n"
              "    pip install -r requirements.txt google-auth-oauthlib", file=sys.stderr)
        return 1

    print("OAuth client from Google Cloud Console -> Credentials -> OAuth client ID (Desktop app).\n")
    client_id = input("client_id: ").strip()
    client_secret = input("client_secret: ").strip()
    if not client_id or not client_secret:
        print("ERROR: both values are required.", file=sys.stderr)
        return 1

    config = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }

    flow = InstalledAppFlow.from_client_config(config, scopes=[SCOPE])
    creds = flow.run_local_server(port=0, prompt="consent")

    if not creds.refresh_token:
        print("\nNo refresh token came back. Revoke the app at "
              "https://myaccount.google.com/permissions and run this again.", file=sys.stderr)
        return 1

    print("\nPut this in google-ads.yaml:\n")
    print(f"refresh_token: {creds.refresh_token}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

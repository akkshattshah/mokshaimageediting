"""
drive_auth.py  -  ONE-TIME Google Drive authorization.

Run this once:   python drive_auth.py
It opens your browser, you pick your Google account and click Allow, and it
saves token.json next to this script. After that the web form can upload to
Drive on its own (no browser needed) until you revoke access.

Uses the drive.file scope, which lets this app upload and share ONLY the files
it creates - it cannot see or touch the rest of your Drive.

If Google shows "Access blocked: this app is not verified", that's expected for
your own project in testing mode: click 'Advanced' -> 'Go to <project> (unsafe)'
-> Allow. (You can also add your email under OAuth consent screen -> Test users.)
"""

import os
from google_auth_oauthlib.flow import InstalledAppFlow

HERE = os.path.dirname(os.path.abspath(__file__))
SCOPES = ["https://www.googleapis.com/auth/drive.file"]


def main():
    client = os.path.join(HERE, "client_secret.json")
    if not os.path.exists(client):
        print("client_secret.json not found in", HERE)
        raise SystemExit(1)
    flow = InstalledAppFlow.from_client_secrets_file(client, SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent")
    with open(os.path.join(HERE, "token.json"), "w", encoding="utf-8") as f:
        f.write(creds.to_json())
    print("\nAuthorized. token.json saved. You can now start the web form:")
    print("    python webform.py")


if __name__ == "__main__":
    main()

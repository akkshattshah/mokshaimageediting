"""
drive_upload.py  -  Upload a zip to Google Drive and return an "anyone with the
link can view" share URL. Used by the web form; can also be run directly to test:

    python drive_upload.py zips/some.zip

Requires token.json (create it once with:  python drive_auth.py).
All uploads go into a single Drive folder ("MokshaImage Handouts") that the app
creates and remembers, so your Drive stays tidy.
"""

import os
import sys
import json

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

HERE = os.path.dirname(os.path.abspath(__file__))
CLIENT = os.path.join(HERE, "client_secret.json")
TOKEN = os.path.join(HERE, "token.json")
FOLDER_REF = os.path.join(HERE, ".drive_folder.json")
SCOPES = ["https://www.googleapis.com/auth/drive.file"]
FOLDER_NAME = "MokshaImage Handouts"


def drive_ready():
    """True if we have a saved authorization to use."""
    return os.path.exists(TOKEN)


def _load_creds():
    if not os.path.exists(TOKEN):
        raise RuntimeError("Google Drive not authorized yet. "
                           "Run:  python drive_auth.py")
    creds = Credentials.from_authorized_user_file(TOKEN, SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            with open(TOKEN, "w", encoding="utf-8") as f:
                f.write(creds.to_json())
        else:
            raise RuntimeError("Google Drive authorization expired. "
                               "Run:  python drive_auth.py")
    return creds


def _service():
    return build("drive", "v3", credentials=_load_creds(),
                 cache_discovery=False)


def _folder_id(service):
    """Find-or-create the handouts folder; remember its id locally."""
    if os.path.exists(FOLDER_REF):
        try:
            fid = json.load(open(FOLDER_REF)).get("id")
            if fid:
                # confirm it still exists / not trashed
                service.files().get(fileId=fid, fields="id,trashed").execute()
                return fid
        except Exception:
            pass
    meta = {"name": FOLDER_NAME,
            "mimeType": "application/vnd.google-apps.folder"}
    f = service.files().create(body=meta, fields="id").execute()
    fid = f["id"]
    with open(FOLDER_REF, "w", encoding="utf-8") as fh:
        json.dump({"id": fid}, fh)
    return fid


def upload_and_share(path):
    """Upload a file, make it link-viewable, return the share URL."""
    service = _service()
    folder = _folder_id(service)
    meta = {"name": os.path.basename(path), "parents": [folder]}
    media = MediaFileUpload(path, mimetype="application/zip", resumable=True)
    f = service.files().create(body=meta, media_body=media,
                               fields="id,webViewLink").execute()
    file_id = f["id"]
    service.permissions().create(
        fileId=file_id, body={"type": "anyone", "role": "reader"}).execute()
    return f.get("webViewLink") or f"https://drive.google.com/file/d/{file_id}/view"


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python drive_upload.py <file>")
        raise SystemExit(1)
    print(upload_and_share(sys.argv[1]))

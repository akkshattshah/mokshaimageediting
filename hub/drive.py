"""Google Drive for the platform. The platform holds the Drive credentials; it
hands out short-lived *upload* links so each worker's companion sends finished
bytes straight to Google (the server never carries the files). Uses the same
token.json / client_secret.json as the desktop tool.
"""

import os
import json

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request, AuthorizedSession
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                 # project root (token.json lives here)
SCOPES = ["https://www.googleapis.com/auth/drive.file"]
FOLDER_NAME = os.environ.get("DRIVE_FOLDER", "PhotoHandout Uploads")
RESUMABLE = "https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable"

_cache = {"folder_id": None}


def _token_path():
    return os.environ.get("GOOGLE_TOKEN_FILE", os.path.join(ROOT, "token.json"))


def _source():
    """Where the Google token comes from: an env var (Railway) or a file (local
    dev). Returns (kind, value)."""
    env = os.environ.get("GOOGLE_TOKEN_JSON")
    if env:
        return "env", json.loads(env)
    path = _token_path()
    if os.path.exists(path):
        return "file", path
    return None, None


def drive_ready():
    kind, _ = _source()
    return kind is not None


def _creds():
    kind, src = _source()
    if kind == "env":
        creds = Credentials.from_authorized_user_info(src, SCOPES)
    elif kind == "file":
        creds = Credentials.from_authorized_user_file(src, SCOPES)
    else:
        raise RuntimeError("No Google credentials. Set GOOGLE_TOKEN_JSON "
                           "(deployed) or provide token.json (local).")
    if not creds.valid and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        if kind == "file":   # only a real file can be persisted
            with open(src, "w", encoding="utf-8") as f:
                f.write(creds.to_json())
    return creds


def _service(creds=None):
    return build("drive", "v3", credentials=creds or _creds(),
                 cache_discovery=False)


def folder_id():
    """Find-or-create the uploads folder, cached in memory."""
    if _cache["folder_id"]:
        return _cache["folder_id"]
    svc = _service()
    q = (f"mimeType='application/vnd.google-apps.folder' and trashed=false "
         f"and name='{FOLDER_NAME}'")
    files = svc.files().list(q=q, fields="files(id)").execute().get("files", [])
    if files:
        fid = files[0]["id"]
    else:
        fid = svc.files().create(
            body={"name": FOLDER_NAME,
                  "mimeType": "application/vnd.google-apps.folder"},
            fields="id").execute()["id"]
    _cache["folder_id"] = fid
    return fid


def init_upload_session(name, mimetype="application/octet-stream"):
    """Start a resumable upload and return the session URL. The worker PUTs the
    file bytes straight to this URL - no Drive credentials on their side."""
    authed = AuthorizedSession(_creds())
    meta = {"name": name, "parents": [folder_id()]}
    r = authed.post(RESUMABLE, json=meta,
                    headers={"X-Upload-Content-Type": mimetype})
    r.raise_for_status()
    return r.headers["Location"]


def upload_file(filename, mimetype, fileobj):
    """Upload a finished file (a file-like object) to the uploads folder, make
    it link-viewable, and return (file_id, share_link)."""
    svc = _service()
    meta = {"name": filename, "parents": [folder_id()]}
    media = MediaIoBaseUpload(fileobj, mimetype=mimetype or "application/octet-stream",
                              resumable=True)
    f = svc.files().create(body=meta, media_body=media,
                           fields="id,webViewLink").execute()
    fid = f["id"]
    try:
        svc.permissions().create(
            fileId=fid, body={"type": "anyone", "role": "reader"}).execute()
    except Exception:  # noqa
        pass
    return fid, f.get("webViewLink") or f"https://drive.google.com/file/d/{fid}/view"


def file_name(file_id, default="file"):
    """The stored name of a Drive file (so a bulk download keeps real filenames
    with their extensions). Falls back to `default` on any error."""
    try:
        authed = AuthorizedSession(_creds())
        r = authed.get(f"https://www.googleapis.com/drive/v3/files/{file_id}",
                       params={"fields": "name"})
        r.raise_for_status()
        return r.json().get("name") or default
    except Exception:  # noqa
        return default


def stream_into_zip(zipf, file_id, arcname):
    """Stream a Drive file's bytes straight into an open zip, a chunk at a time
    (never loads the whole file into memory) — mirrors s3source.fetch_into_zip."""
    authed = AuthorizedSession(_creds())
    r = authed.get(f"https://www.googleapis.com/drive/v3/files/{file_id}",
                   params={"alt": "media"}, stream=True)
    r.raise_for_status()
    with zipf.open(arcname, "w") as dest:
        for chunk in r.iter_content(1024 * 1024):
            if chunk:
                dest.write(chunk)


def delete_file(file_id):
    """Best-effort delete of a file the app created (used to drop a worker's
    version once the QC's corrected version replaces it — so each photo is
    stored only once). Returns True on success."""
    try:
        _service().files().delete(fileId=file_id).execute()
        return True
    except Exception:  # noqa
        return False


def finalize(file_id):
    """After the worker uploads, make it link-viewable and return the link."""
    svc = _service()
    try:
        svc.permissions().create(
            fileId=file_id, body={"type": "anyone", "role": "reader"}).execute()
    except Exception:  # noqa
        pass
    f = svc.files().get(fileId=file_id, fields="id,webViewLink").execute()
    return f.get("webViewLink") or f"https://drive.google.com/file/d/{file_id}/view"

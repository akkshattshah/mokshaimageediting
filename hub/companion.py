"""Worker companion — runs on a retoucher's own PC.

  python -m hub.companion --login yash --password *** download

Signs in to the platform, sees what's assigned, and downloads that worker's
photos into ./worker_pool/. It fetches files over short-lived links the platform
issues, so the S3 key never lives on the worker's machine. Uploading finished
work (to Drive) is the next step.
"""

import argparse
import base64
import glob
import json
import os
import urllib.request

DEFAULT_HUB = os.environ.get("HUB_URL", "http://127.0.0.1:5001")


def _call(hub, path, login, password, method="GET", body=None):
    url = hub.rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    token = base64.b64encode(f"{login}:{password}".encode()).decode()
    req.add_header("Authorization", "Basic " + token)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise SystemExit(f"Platform said {e.code}: {e.read().decode()[:200]}")
    except urllib.error.URLError as e:
        raise SystemExit(f"Can't reach the platform at {hub} ({e.reason}). "
                         f"Is it running / is the address right?")


def _fetch(url, dest):
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".part"
    urllib.request.urlretrieve(url, tmp)
    os.replace(tmp, dest)


def download(args):
    mine = _call(args.hub, "/api/my-work", args.login, args.password)
    print(f"Signed in as {mine['worker']}.  Assignments: {len(mine['assignments'])}\n")

    for a in mine["assignments"]:
        todo = [p for p in a["photos"] if p["status"] == "assigned"]
        print(f"Assignment {a['assignment_id']} · {a['brand']} — "
              f"{len(a['photos'])} photos, {len(todo)} still to download")
        if not todo:
            continue

        urls = _call(args.hub, f"/api/assignment/{a['assignment_id']}/download-urls",
                     args.login, args.password)
        got, i = [], 0
        total = sum(len(p["files"]) for p in urls["photos"])
        for p in urls["photos"]:
            for f in p["files"]:
                i += 1
                dest = os.path.join(args.dest, *f["path"].split("/"))
                _fetch(f["url"], dest)
                print(f"  [{i}/{total}] {f['path'].split('/')[-1]}")
            got.append(p["photo_id"])

        if got:
            res = _call(args.hub, f"/api/assignment/{a['assignment_id']}/downloaded",
                        args.login, args.password, method="POST",
                        body={"photo_ids": got})
            print(f"  -> reported {res['marked']} downloaded\n")

    print(f"Done. Files are in ./{args.dest}/")


def _mimetype(path):
    p = path.lower()
    if p.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    if p.endswith(".png"):
        return "image/png"
    if p.endswith(".psd"):
        return "image/vnd.adobe.photoshop"
    return "application/octet-stream"


def upload(args):
    """Send finished files back. For each downloaded photo, uploads the finished
    output from ./worker_pool (here we send the retouched image). The platform
    hands out per-file upload links; bytes go straight to Google Drive."""
    mine = _call(args.hub, "/api/my-work", args.login, args.password)
    print(f"Signed in as {mine['worker']}.\n")

    for a in mine["assignments"]:
        ready = [p for p in a["photos"] if p["status"] == "downloaded"]
        print(f"Assignment {a['assignment_id']} · {a['brand']} — "
              f"{len(ready)} photo(s) ready to upload")
        if not ready:
            continue

        # pick the finished file per photo (prefer the image over the .psd)
        to_send = []
        for p in ready:
            base = os.path.join(args.dest, *p["photo_id"].split("/"))
            found = [c for c in glob.glob(base + "*") if not c.endswith(".part")]
            imgs = [c for c in found if c.lower().endswith((".jpg", ".jpeg", ".png"))]
            chosen = (imgs or found)[:1]
            for c in chosen:
                to_send.append((p["photo_id"], c, os.path.basename(c), _mimetype(c)))
        if not to_send:
            print("  (no finished files found locally)")
            continue

        sess = _call(args.hub, f"/api/assignment/{a['assignment_id']}/upload-sessions",
                     args.login, args.password, method="POST",
                     body={"files": [{"photo_id": pid, "filename": fn, "mimetype": mt}
                                      for (pid, _p, fn, mt) in to_send]})["sessions"]
        smap = {(s["photo_id"], s["filename"]): s["session_url"] for s in sess}

        items, i = [], 0
        for (pid, path, fn, mt) in to_send:
            url = smap.get((pid, fn))
            if not url:
                continue
            i += 1
            with open(path, "rb") as fh:
                data = fh.read()
            req = urllib.request.Request(url, data=data, method="PUT")
            req.add_header("Content-Type", mt)
            with urllib.request.urlopen(req, timeout=1200) as resp:
                res = json.loads(resp.read().decode())
            items.append({"photo_id": pid, "file_id": res["id"]})
            print(f"  [{i}/{len(to_send)}] uploaded {fn}")

        done = _call(args.hub, f"/api/assignment/{a['assignment_id']}/uploaded",
                     args.login, args.password, method="POST", body={"items": items})
        pr = done["progress"]
        print(f"  -> {done['marked']} marked done. "
              f"Progress: {pr['uploaded']}/{pr['assigned']}"
              f"{' — COMPLETE' if pr['complete'] else ''}\n")

    print("Upload finished.")


def status(args):
    mine = _call(args.hub, "/api/my-work", args.login, args.password)
    print(f"{mine['worker']} — assignments:")
    for a in mine["assignments"]:
        by = {}
        for p in a["photos"]:
            by[p["status"]] = by.get(p["status"], 0) + 1
        parts = ", ".join(f"{k}: {v}" for k, v in sorted(by.items()))
        print(f"  #{a['assignment_id']} {a['brand']}: {parts}")


def main():
    ap = argparse.ArgumentParser(description="Photo Handout worker companion")
    ap.add_argument("action", choices=["download", "upload", "status"])
    ap.add_argument("--hub", default=DEFAULT_HUB)
    ap.add_argument("--login", required=True)
    ap.add_argument("--password", required=True)
    ap.add_argument("--dest", default="worker_pool")
    args = ap.parse_args()
    {"download": download, "upload": upload, "status": status}[args.action](args)


if __name__ == "__main__":
    main()

"""Read-only view of the client's S3 bucket, used to reserve photos for an
assignment. Self-contained (mirrors the desktop tool's photo-id logic) so the
platform can be deployed on its own. Credentials come from the environment:
AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY on Railway, or ~/.aws locally - boto3
picks up either automatically.
"""

import datetime as dt
import os
import re

import boto3

REGION = os.environ.get("AWS_REGION", "ap-south-1")
BUCKET = os.environ.get("S3_BUCKET", "carcutter-mumbai")
BASE = os.environ.get("S3_PREFIX", "download-retouching-files/")

# where the client's pipeline stores each finished photo as a flat JPG:
# <DONE_BASE><brand>/<date>/<name>.jpg for the photo <brand>/<date>/.../<name>.
# Empty disables the check (every photo in S3 counts as pending).
DONE_BASE = os.environ.get("S3_DONE_PREFIX", "upload-retouching-jpg/")

IMG_EXTS = (".psd", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp")
MAX_DATES = 8      # assignments draw from a brand's newest this-many date folders

_s3 = boto3.client("s3", region_name=REGION)
_pag = _s3.get_paginator("list_objects_v2")


def _photo_id(key):
    """The base id shared by a photo's files (.psd and its _orig .jpg)."""
    folder, _, name = key.rpartition("/")
    low = name.lower()
    for ext in IMG_EXTS:
        if low.endswith(ext):
            name = name[: -len(ext)]
            low = low[: -len(ext)]
            break
    if low.endswith("_orig"):
        name = name[: -len("_orig")]
    return f"{folder}/{name}"


def local_pid(pid):
    """Strip the bucket prefix so ids match what we store in the DB."""
    return pid[len(BASE):] if pid.startswith(BASE) else pid


def list_brands():
    out = []
    for page in _pag.paginate(Bucket=BUCKET, Prefix=BASE, Delimiter="/"):
        for p in page.get("CommonPrefixes", []):
            name = p["Prefix"][len(BASE):].strip("/")
            if name:
                out.append(name)
    return sorted(out)


_DATE_DIR = re.compile(r"(\d{8})(?:[_-][A-Za-z0-9]+)*")


def _is_date(name):
    """Date folders are named YYYYMMDD (e.g. 20260716), optionally with a
    suffix such as 20260716_2 for a second delivery that day."""
    m = _DATE_DIR.fullmatch(name)
    if not m:
        return False
    try:
        dt.datetime.strptime(m.group(1), "%Y%m%d")
    except ValueError:
        return False
    return True


def date_folders(brand):
    """A brand's date folders, newest first. Paginated (one S3 call returns
    at most 1000, oldest first), and anything not named like a date is
    skipped - it would otherwise sort ahead of every real date."""
    dates = []
    for page in _pag.paginate(Bucket=BUCKET, Prefix=f"{BASE}{brand}/",
                              Delimiter="/"):
        dates += [p["Prefix"].split("/")[-2]
                  for p in page.get("CommonPrefixes", [])]
    return sorted((d for d in dates if _is_date(d)), reverse=True)


def files_for_photo(photo_id):
    """The S3 keys of a photo's files (.psd + _orig.jpg), from its pool-relative
    id. Both files share the id as a prefix, so one list call finds them - but
    so do other photos whose names merely start with it ('car-1' / 'car-10'),
    so only keys whose own photo id is this one are kept. Paginated: those
    other photos can push this one's files past the first 1000 keys."""
    prefix = BASE + photo_id
    return [o["Key"]
            for page in _pag.paginate(Bucket=BUCKET, Prefix=prefix)
            for o in page.get("Contents", [])
            if not o["Key"].endswith("/")
            and o["Key"].lower().endswith(IMG_EXTS)
            and local_pid(_photo_id(o["Key"])) == photo_id]


def presigned_get(key, expires=3600, filename=None):
    """A short-lived download link for one object. Lets a worker fetch a file
    over plain HTTPS without ever holding the S3 key. When `filename` is given,
    the link forces a browser 'save as' with that name (so a Download button in
    the page downloads the file instead of opening it)."""
    params = {"Bucket": BUCKET, "Key": key}
    if filename:
        params["ResponseContentDisposition"] = f'attachment; filename="{filename}"'
    return _s3.generate_presigned_url(
        "get_object", Params=params, ExpiresIn=expires)


def fetch_into_zip(zipf, key, arcname):
    """Stream one S3 object straight into an open zip, a chunk at a time (never
    loads the whole file into memory)."""
    obj = _s3.get_object(Bucket=BUCKET, Key=key)
    with zipf.open(arcname, "w") as dest:
        for chunk in obj["Body"].iter_chunks(1024 * 1024):
            dest.write(chunk)


def folder_photo_ids(brand, date):
    """Photo ids (pool-relative) of every photo in one of a brand's date
    folders."""
    ids = set()
    for page in _pag.paginate(Bucket=BUCKET, Prefix=f"{BASE}{brand}/{date}/"):
        for o in page.get("Contents", []):
            k = o["Key"]
            if k.endswith("/") or not k.lower().endswith(IMG_EXTS):
                continue
            ids.add(local_pid(_photo_id(k)))
    return ids


def finished_names(brand, date):
    """Names of the photos in one of a brand's date folders that are already
    retouched: the client's pipeline stores each as <name>.jpg under
    DONE_BASE<brand>/<date>/."""
    if not DONE_BASE:
        return set()
    names = set()
    for page in _pag.paginate(Bucket=BUCKET, Prefix=f"{DONE_BASE}{brand}/{date}/"):
        for o in page.get("Contents", []):
            k = o["Key"]
            if not k.endswith("/") and k.lower().endswith(IMG_EXTS):
                names.add(_photo_id(k).rsplit("/", 1)[-1])
    return names


def pending_photo_ids(brand, date):
    """Photo ids in a date folder that still need retouching - everything in
    the folder minus the photos that already have a finished JPG. This is
    what the dropdown counts and what Assign hands out."""
    done = finished_names(brand, date)
    return {pid for pid in folder_photo_ids(brand, date)
            if pid.rsplit("/", 1)[-1] not in done}


def available_photo_ids(brand, exclude, want=None, max_dates=MAX_DATES):
    """Photo ids (pool-relative) still to be retouched in S3 for `brand` that
    are not in `exclude`, newest dates first. Stops once `want` are found."""
    exclude = exclude or set()
    picked = []
    for d in date_folders(brand)[:max_dates] if max_dates else date_folders(brand):
        for pid in sorted(pending_photo_ids(brand, d)):
            if pid in exclude:
                continue
            picked.append(pid)
            if want and len(picked) >= want:
                return picked
    return picked

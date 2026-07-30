"""Read-only view of the client's S3 bucket, used to reserve photos for an
assignment. Self-contained (mirrors the desktop tool's photo-id logic) so the
platform can be deployed on its own. Credentials come from the environment:
AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY on Railway, or ~/.aws locally - boto3
picks up either automatically.
"""

import os

import boto3

REGION = os.environ.get("AWS_REGION", "ap-south-1")
BUCKET = os.environ.get("S3_BUCKET", "carcutter-mumbai")
BASE = os.environ.get("S3_PREFIX", "download-retouching-files/")

IMG_EXTS = (".psd", ".jpeg", ".jpg", ".png", ".tif", ".tiff")

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


def date_folders(brand):
    resp = _s3.list_objects_v2(
        Bucket=BUCKET, Prefix=f"{BASE}{brand}/", Delimiter="/")
    dates = [p["Prefix"].split("/")[-2] for p in resp.get("CommonPrefixes", [])]
    return sorted(dates, reverse=True)


def files_for_photo(photo_id):
    """The S3 keys of a photo's files (.psd + _orig.jpg), from its pool-relative
    id. Both files share the id as a prefix, so one list call finds them."""
    prefix = BASE + photo_id
    resp = _s3.list_objects_v2(Bucket=BUCKET, Prefix=prefix)
    return [o["Key"] for o in resp.get("Contents", [])
            if not o["Key"].endswith("/")]


def presigned_get(key, expires=3600):
    """A short-lived download link for one object. Lets the worker's companion
    fetch a file over plain HTTPS without ever holding the S3 key."""
    return _s3.generate_presigned_url(
        "get_object", Params={"Bucket": BUCKET, "Key": key}, ExpiresIn=expires)


def available_photo_ids(brand, exclude, want=None, max_dates=8):
    """Photo ids (pool-relative) available in S3 for `brand` that are not in
    `exclude`, newest dates first. Stops once `want` are found."""
    exclude = exclude or set()
    picked = []
    for d in date_folders(brand)[:max_dates] if max_dates else date_folders(brand):
        ids = set()
        for page in _pag.paginate(Bucket=BUCKET, Prefix=f"{BASE}{brand}/{d}/"):
            for o in page.get("Contents", []):
                k = o["Key"]
                if k.endswith("/") or not k.lower().endswith(IMG_EXTS):
                    continue
                ids.add(local_pid(_photo_id(k)))
        for pid in sorted(ids):
            if pid in exclude:
                continue
            picked.append(pid)
            if want and len(picked) >= want:
                return picked
    return picked

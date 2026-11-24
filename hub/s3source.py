"""Read-only view of the client's S3 bucket, used to reserve photos for an
assignment. Self-contained (mirrors the desktop tool's photo-id logic) so the
platform can be deployed on its own. Credentials come from the environment:
AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY on Railway, or ~/.aws locally - boto3
picks up either automatically.
"""

import datetime as dt
import hashlib
import os
import re
import struct
import tempfile
import zipfile
import zlib
from concurrent.futures import ThreadPoolExecutor

import boto3
from botocore.config import Config

REGION = os.environ.get("AWS_REGION", "ap-south-1")
BUCKET = os.environ.get("S3_BUCKET", "carcutter-mumbai")
BASE = os.environ.get("S3_PREFIX", "download-retouching-files/")

# where the client's pipeline stores each finished photo as a flat JPG:
# <DONE_BASE><brand>/<date>/<name>.jpg for the photo <brand>/<date>/.../<name>.
# Empty disables the check (every photo in S3 counts as pending).
DONE_BASE = os.environ.get("S3_DONE_PREFIX", "upload-retouching-jpg/")

IMG_EXTS = (".psd", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp")
MAX_DATES = 8      # assignments draw from a brand's newest this-many date folders

# On Railway a read from S3 can freeze mid-file with no error. Give up on a
# silent connection after 20 s (default 60 s) so it can be retried, and keep
# connections alive with TCP keepalives.
_s3 = boto3.client("s3", region_name=REGION, config=Config(
    connect_timeout=10, read_timeout=20, tcp_keepalive=True,
    retries={"max_attempts": 5, "mode": "standard"}))
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
    return [o["Key"] for o in photo_files(photo_id)]


def photo_files(photo_id):
    """files_for_photo, with each file's S3 listing entry (Key, Size, ETag,
    LastModified)."""
    prefix = BASE + photo_id
    return [o
            for page in _s3.get_paginator("list_objects_v2").paginate(
                Bucket=BUCKET, Prefix=prefix)
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


class _ZipSink:
    """Write-only output for zipfile (it handles unseekable output) that keeps
    what was written until zip_photos hands it on."""

    def __init__(self):
        self._parts = []

    def write(self, data):
        self._parts.append(bytes(data))
        return len(data)

    def flush(self):
        pass

    def drain(self):
        if self._parts:
            data = b"".join(self._parts)
            self._parts.clear()
            yield data


FETCH_TRIES = 3


def _fetch(key, chunk):
    """The whole S3 object in a temp file (memory, then disk past 8 MB).
    Fetched in full before any of it is sent, so S3 is never left waiting on
    a slow browser, and a read that freezes or breaks is simply started over
    (up to FETCH_TRIES times) - nothing of this file has been sent yet.
    Returns (temp file, CRC-32, size)."""
    for attempt in range(1, FETCH_TRIES + 1):
        tmp = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024)
        try:
            body = _s3.get_object(Bucket=BUCKET, Key=key)["Body"]
            crc = size = 0
            for part in body.iter_chunks(chunk):
                tmp.write(part)
                crc = zlib.crc32(part, crc)
                size += len(part)
            tmp.seek(0)
            return tmp, crc, size
        except Exception:
            tmp.close()
            if attempt == FETCH_TRIES:
                raise


def zip_photos(photo_ids, chunk=1024 * 1024):
    """One zip of these photos' files (kept in their folders, pool-relative),
    yielded file by file as each is fetched from S3 - so a download starts
    after the first file instead of after the whole zip is built. The next
    file is fetched in the background while the current one is being sent."""
    keys = (k for pid in photo_ids for k in files_for_photo(pid))
    sink = _ZipSink()
    pool = ThreadPoolExecutor(max_workers=1)
    key = next(keys, None)
    pending = pool.submit(_fetch, key, chunk) if key else None
    try:
        with zipfile.ZipFile(sink, "w", zipfile.ZIP_STORED) as zf:
            while pending:
                src, name = pending.result()[0], local_pid(key)
                key = next(keys, None)
                pending = pool.submit(_fetch, key, chunk) if key else None
                with src, zf.open(name, "w") as dest:
                    while part := src.read(chunk):
                        dest.write(part)
                        yield from sink.drain()
                yield from sink.drain()
        yield from sink.drain()        # the zip's closing directory
    finally:                           # download finished or browser gave up
        if pending:
            try:
                pending.result()[0].close()
            except Exception:
                pass
        pool.shutdown(wait=False)


# ---- resumable zip --------------------------------------------------------
# A plain (stored, uncompressed) zip whose every byte position is worked out
# from the file names and sizes before anything is sent. So the same batch
# always gives byte-for-byte the same zip, and a browser that lost the
# connection can ask for "the rest, from byte N" (an HTTP Range request) and
# continue instead of starting over. Each file's CRC-32 goes in its header;
# it is computed when the file is read and remembered (crc_cache) for the
# zip's closing directory and for later resumes.

_LOCAL = struct.Struct("<IHHHHHIIIHH")           # local file header, 30 bytes
_CENTRAL = struct.Struct("<IHHHHHHIIIHHHHHII")   # central directory entry, 46
_END = struct.Struct("<IHHHHIIH")                # end of central directory, 22
_ZIP32_MAX = 0xFFFFFFFF


def _dos_time(when):
    when = max(when, dt.datetime(1980, 1, 1, tzinfo=when.tzinfo))
    return (when.hour << 11 | when.minute << 5 | when.second // 2,
            (when.year - 1980) << 9 | when.month << 5 | when.day)


class _Entry:
    def __init__(self, obj, offset):
        self.key, self.size = obj["Key"], obj["Size"]
        self.etag = str(obj.get("ETag", "")).strip('"')
        self.name = local_pid(self.key).encode("utf-8")
        self.flags = 0 if self.name.isascii() else 0x800      # UTF-8 name
        self.time, self.date = _dos_time(obj["LastModified"])
        self.modified = obj["LastModified"]
        self.offset = offset                                  # local header
        self.data = offset + _LOCAL.size + len(self.name)     # first data byte
        self.end = self.data + self.size                      # one past last

    def local_header(self, crc):
        return _LOCAL.pack(0x04034B50, 20, self.flags, 0, self.time, self.date,
                           crc, self.size, self.size, len(self.name), 0) + self.name

    def central(self, crc):
        return _CENTRAL.pack(0x02014B50, 20, 20, self.flags, 0, self.time,
                             self.date, crc, self.size, self.size, len(self.name),
                             0, 0, 0, 0, 0, self.offset) + self.name


class ZipPlan:
    """The exact layout of the zip for a list of photos."""

    def __init__(self, objs):
        self.entries, pos = [], 0
        for o in objs:
            e = _Entry(o, pos)
            self.entries.append(e)
            pos = e.end
        self.cd_offset = pos
        self.cd_size = sum(_CENTRAL.size + len(e.name) for e in self.entries)
        self.size = self.cd_offset + self.cd_size + _END.size
        sig = hashlib.sha1(b"zip-v1")
        for e in self.entries:
            sig.update(f"{e.key}|{e.size}|{e.etag}|{e.modified.isoformat()}\n".encode())
        self.etag = f'"{sig.hexdigest()}"'
        self.modified = max((e.modified for e in self.entries), default=None)

    @property
    def fits_zip32(self):
        return self.size < _ZIP32_MAX and len(self.entries) < 0xFFFF

    def stream(self, start, end, cache, chunk=1024 * 1024):
        """Bytes start..end (inclusive) of the zip. Files that overlap the
        range are read from S3 whole (with retries); the next one is fetched
        in the background while the current one is sent."""
        todo = [e for e in self.entries if e.end > start and e.offset <= end]
        crcs = {}
        pool = ThreadPoolExecutor(max_workers=1)
        it = iter(todo)
        cur = next(it, None)
        pending = pool.submit(_fetch, cur.key, chunk) if cur else None
        try:
            while pending:
                src, crc, size = pending.result()
                e = cur
                cur = next(it, None)
                pending = pool.submit(_fetch, cur.key, chunk) if cur else None
                with src:
                    if size != e.size:
                        raise RuntimeError(f"{e.key} changed in S3 during the download")
                    crcs[e.key] = crc
                    cache.put(e.key, e.etag, e.size, crc)
                    yield from _part(e.local_header(crc), e.offset, start, end)
                    a, b = max(start, e.data), min(end + 1, e.end)
                    if a < b:
                        src.seek(a - e.data)
                        left = b - a
                        while left:
                            part = src.read(min(chunk, left))
                            if not part:
                                raise RuntimeError(f"{e.key} came back short")
                            left -= len(part)
                            yield part
            if end >= self.cd_offset:
                yield from _part(self._directory(crcs, cache, chunk),
                                 self.cd_offset, start, end)
        finally:
            if pending:
                try:
                    pending.result()[0].close()
                except Exception:
                    pass
            pool.shutdown(wait=False)

    def _directory(self, crcs, cache, chunk):
        cd = []
        for e in self.entries:
            crc = crcs.get(e.key)
            if crc is None:
                crc = cache.get(e.key, e.etag, e.size)
            if crc is None:                  # not remembered: read it once more
                src, crc, size = _fetch(e.key, chunk)
                src.close()
                if size != e.size:
                    raise RuntimeError(f"{e.key} changed in S3 during the download")
                cache.put(e.key, e.etag, e.size, crc)
            cd.append(e.central(crc))
        cd = b"".join(cd)
        n = len(self.entries)
        return cd + _END.pack(0x06054B50, 0, 0, n, n, len(cd), self.cd_offset, 0)


def _part(blob, offset, start, end):
    """The piece of `blob` (which sits at `offset` in the zip) inside start..end."""
    a, b = max(start - offset, 0), min(end + 1 - offset, len(blob))
    if a < b:
        yield blob[a:b]


def download_links(photo_ids, expires=12 * 3600):
    """A direct S3 download link for every file of these photos (sorted), so
    the browser fetches them straight from S3 - not through this server, so
    hosting time limits never cut them, and they come from Mumbai directly.
    Each link saves under the file's own name (what upload matching uses).
    Links last `expires` seconds (long enough to resume a slow file)."""
    pids = sorted(set(photo_ids))
    with ThreadPoolExecutor(max_workers=8) as pool:
        lists = list(pool.map(photo_files, pids))
    keys = [o["Key"] for files in lists for o in sorted(files, key=lambda o: o["Key"])]
    return [{"name": k.rsplit("/", 1)[-1],
             "url": presigned_get(k, expires=expires, filename=k.rsplit("/", 1)[-1])}
            for k in keys]


def zip_plan(photo_ids):
    """The ZipPlan for these photos (sorted, so the zip is always the same),
    or None if it would be too big for a plain zip (4 GB) - then the caller
    falls back to zip_photos."""
    pids = sorted(set(photo_ids))
    with ThreadPoolExecutor(max_workers=8) as pool:
        lists = list(pool.map(photo_files, pids))
    objs = [o for files in lists for o in sorted(files, key=lambda o: o["Key"])]
    plan = ZipPlan(objs)
    return plan if plan.fits_zip32 else None


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

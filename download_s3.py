"""
download_s3.py  -  Download retouching images for ONE brand from the
carcutter-mumbai S3 bucket, most-recent-date-first.

Usage examples (run from the MokshaImage folder):

    # See what the newest date for a brand contains, download nothing:
    python download_s3.py catchall_ireland --dry-run

    # Download the newest date's files for a brand:
    python download_s3.py catchall_ireland

    # Download the 3 most recent dates:
    python download_s3.py pixelconcept --recent 3

    # Download one specific date:
    python download_s3.py hil --date 20260708

    # List the available date folders for a brand and exit:
    python download_s3.py hil --list-dates

By default it grabs BOTH file types per photo (the .psd working file and the
_orig .jpg/.jpeg original). Use --types to change that.

Files are saved under  ./pool/<brand>/<date>/...  keeping the S3 folder
structure, and anything already downloaded (same size) is skipped, so re-running
is safe and only pulls new files.
"""

import argparse
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.exceptions import ClientError

# ---- fixed config for this client's bucket -------------------------------
REGION = "ap-south-1"
BUCKET = "carcutter-mumbai"
BASE = "download-retouching-files/"
DEFAULT_DEST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pool")
# extensions considered "both files per photo"
TYPE_PRESETS = {
    "both": (".psd", ".jpg", ".jpeg"),
    "orig": (".jpg", ".jpeg"),
    "psd": (".psd",),
    "all": None,  # no filter
}
# --------------------------------------------------------------------------

s3 = boto3.client("s3", region_name=REGION)
_paginator = s3.get_paginator("list_objects_v2")
_transfer_cfg = TransferConfig(multipart_threshold=16 * 1024 * 1024,
                               max_concurrency=4)
_print_lock = threading.Lock()


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}"
        n /= 1024


def brand_prefix(brand):
    return f"{BASE}{brand}/"


def list_date_folders(brand):
    """Return date-folder names (e.g. '20260716') for a brand, newest first."""
    resp = s3.list_objects_v2(Bucket=BUCKET, Prefix=brand_prefix(brand),
                              Delimiter="/")
    dates = [p["Prefix"].split("/")[-2] for p in resp.get("CommonPrefixes", [])]
    return sorted(dates, reverse=True)


def resolve_dates(brand, args):
    all_dates = list_date_folders(brand)
    if not all_dates:
        return []
    if args.date:
        if args.date not in all_dates:
            print(f"  ! date {args.date} not found for {brand}. "
                  f"Available (newest first): {all_dates[:8]}")
            return []
        return [args.date]
    if args.recent is not None:
        return all_dates[: max(1, args.recent)]
    if args.photos is not None:
        return all_dates            # gather newest-first until N photos reached
    return all_dates[:1]


def list_files(brand, dates, exts):
    """Yield (key, size) for all matching files under the given dates."""
    for d in dates:
        prefix = f"{brand_prefix(brand)}{d}/"
        for page in _paginator.paginate(Bucket=BUCKET, Prefix=prefix):
            for o in page.get("Contents", []):
                key = o["Key"]
                if key.endswith("/"):
                    continue
                if exts and not key.lower().endswith(exts):
                    continue
                yield key, o["Size"]


def photo_id(key):
    """Base identity shared by a photo's files: the .psd and its _orig .jpg/.jpeg
    map to the same id, so they count as ONE photo."""
    folder, _, name = key.rpartition("/")
    low = name.lower()
    for ext in (".psd", ".jpeg", ".jpg", ".png", ".tif", ".tiff"):
        if low.endswith(ext):
            name = name[: -len(ext)]
            low = low[: -len(ext)]
            break
    if low.endswith("_orig"):
        name = name[: -len("_orig")]
    return f"{folder}/{name}"


def group_photos(files):
    """Group (key,size) files into photos. Returns [(photo_id, [(key,size),...])],
    sorted deterministically by photo_id."""
    groups = {}
    for key, size in files:
        groups.setdefault(photo_id(key), []).append((key, size))
    return [(pid, groups[pid]) for pid in sorted(groups)]


def delivered_pids(dest):
    """Photo ids already handed out (from the distribute ledger). These are
    stored pool-relative, so we compare after stripping the BASE prefix. Lets us
    avoid re-downloading photos that were delivered and then deleted locally."""
    import json
    ledger = os.path.join(dest, "_ledger.json")
    if not os.path.exists(ledger):
        return set()
    try:
        with open(ledger, "r", encoding="utf-8") as f:
            return set(json.load(f).keys())
    except Exception:
        return set()


def local_pid(s3_photo_id):
    """Convert an S3-key photo_id to the pool-relative id used by the ledger."""
    return s3_photo_id[len(BASE):] if s3_photo_id.startswith(BASE) else s3_photo_id


def local_path(dest, key):
    # strip the BASE prefix so local tree starts at <brand>/<date>/...
    rel = key[len(BASE):] if key.startswith(BASE) else key
    return os.path.join(dest, *rel.split("/"))


def list_brands():
    """Every brand folder that exists in S3 under the retouching prefix."""
    out = []
    for page in _paginator.paginate(Bucket=BUCKET, Prefix=BASE, Delimiter="/"):
        for p in page.get("CommonPrefixes", []):
            name = p["Prefix"][len(BASE):].strip("/")
            if name:
                out.append(name)
    return sorted(out)


def photo_present_locally(dest, grp):
    """True if every file of this photo is already downloaded."""
    return all(os.path.exists(local_path(dest, k)) for k, _ in grp)


def select_new_photos(brand, count=None, dest=DEFAULT_DEST, types="both",
                      max_dates=None):
    """Photos in S3 for this brand that we neither already have locally nor
    have handed out. Newest dates first. Stops early once `count` is reached;
    `max_dates` limits how far back we look (keeps the check fast)."""
    exts = TYPE_PRESETS[types]
    already = delivered_pids(dest)
    dates = list_date_folders(brand)
    if max_dates:
        dates = dates[:max_dates]
    picked = []
    for d in dates:
        for pid, grp in group_photos(list(list_files(brand, [d], exts))):
            if local_pid(pid) in already:
                continue
            if photo_present_locally(dest, grp):
                continue
            picked.append((pid, grp))
            if count is not None and len(picked) >= count:
                return picked
    return picked


def count_new_photos(brand, dest=DEFAULT_DEST, types="both", max_dates=5):
    """How many not-yet-downloaded, not-yet-delivered photos are waiting in S3
    across the newest `max_dates` date folders."""
    return len(select_new_photos(brand, None, dest, types, max_dates))


def download_new_photos(brand, count, dest=DEFAULT_DEST, types="both",
                        workers=6, progress=None):
    """Download up to `count` brand-new photos. progress(done, total,
    done_bytes, total_bytes) is called as files complete."""
    picked = select_new_photos(brand, count, dest, types)
    files = [(k, s) for _, grp in picked for (k, s) in grp]
    total_files = len(files)
    total_bytes = sum(s for _, s in files)
    done = errors = 0
    done_bytes = 0
    if progress:
        progress(0, total_files, 0, total_bytes)
    if files:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(download_one, k, s, dest) for k, s in files]
            for f in as_completed(futs):
                status, _key, extra = f.result()
                done += 1
                if status == "err":
                    errors += 1
                else:
                    done_bytes += extra
                if progress:
                    progress(done, total_files, done_bytes, total_bytes)
    return {"photos": len(picked), "files": total_files,
            "bytes": total_bytes, "errors": errors}


def download_one(key, size, dest):
    path = local_path(dest, key)
    if os.path.exists(path) and os.path.getsize(path) == size:
        return ("skip", key, size)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".part"
    try:
        s3.download_file(BUCKET, key, tmp, Config=_transfer_cfg)
        os.replace(tmp, path)
        return ("ok", key, size)
    except Exception as e:  # noqa
        if os.path.exists(tmp):
            os.remove(tmp)
        return ("err", key, str(e))


def main():
    ap = argparse.ArgumentParser(description="Download one brand's retouching "
                                             "images from S3, newest date first.")
    ap.add_argument("brand", help="brand folder name, e.g. catchall_ireland")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--date", help="download exactly this date (YYYYMMDD)")
    g.add_argument("--recent", type=int, default=None,
                   help="download the N most recent dates (default: newest 1)")
    ap.add_argument("-n", "--photos", type=int, default=None,
                    help="download exactly N photos (image+psd pair = 1 photo); "
                         "fills from the newest date, spilling into older dates "
                         "only if needed")
    ap.add_argument("--types", choices=list(TYPE_PRESETS), default="both",
                    help="which files: both(default) | orig | psd | all")
    ap.add_argument("--dest", default=DEFAULT_DEST,
                    help=f"local destination root (default {DEFAULT_DEST})")
    ap.add_argument("--workers", type=int, default=6,
                    help="parallel downloads (default 6)")
    ap.add_argument("--list-dates", action="store_true",
                    help="just list available dates for the brand and exit")
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would be downloaded, download nothing")
    args = ap.parse_args()

    # verify brand exists
    if s3.list_objects_v2(Bucket=BUCKET, Prefix=brand_prefix(args.brand),
                          MaxKeys=1).get("KeyCount", 0) == 0:
        print(f"Brand '{args.brand}' not found or empty under {BASE}")
        sys.exit(1)

    if args.list_dates:
        dates = list_date_folders(args.brand)
        print(f"{args.brand}: {len(dates)} date folders (newest first):")
        for d in dates:
            print("   ", d)
        return

    dates = resolve_dates(args.brand, args)
    if not dates:
        print("Nothing to do.")
        sys.exit(1)

    exts = TYPE_PRESETS[args.types]

    # Gather photos newest-date-first. A "photo" = its files sharing a base name
    # (the .psd working file + its _orig .jpg/.jpeg), counted as ONE.
    # Skip photos already handed out (in the ledger) so delivered-and-deleted
    # photos are never re-downloaded and never counted toward --photos N.
    already = delivered_pids(args.dest)
    skipped_delivered = 0
    photos = []
    used_dates = []
    for d in dates:
        day_photos = group_photos(list(list_files(args.brand, [d], exts)))
        kept = [(pid, grp) for pid, grp in day_photos
                if local_pid(pid) not in already]
        skipped_delivered += len(day_photos) - len(kept)
        if kept:
            used_dates.append(d)
        photos.extend(kept)
        if args.photos is not None and len(photos) >= args.photos:
            break

    if args.photos is not None:
        if len(photos) < args.photos:
            print(f"  ! only {len(photos)} photos available "
                  f"(requested {args.photos}) across dates {used_dates}")
        photos = photos[: args.photos]

    files = [(k, s) for _, grp in photos for (k, s) in grp]
    total = sum(s for _, s in files)

    print(f"Brand:  {args.brand}")
    print(f"Dates:  {', '.join(used_dates) or '(none)'}")
    print(f"Types:  {args.types}  {exts or '(all)'}")
    print(f"Photos: {len(photos)}   Files: {len(files)}   Total size: {human(total)}")
    if skipped_delivered:
        print(f"Skipped {skipped_delivered} already-delivered photo(s).")
    print(f"Dest:   {args.dest}")

    if not files:
        print("No matching files.")
        return
    if args.dry_run:
        print("\n-- dry run, nothing downloaded. Sample:")
        for k, s in files[:10]:
            print(f"   {human(s):>9}  {k[len(BASE):]}")
        if len(files) > 10:
            print(f"   ... and {len(files) - 10} more")
        return

    print("\nDownloading...")
    ok = skip = err = 0
    done_bytes = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = [ex.submit(download_one, k, s, args.dest) for k, s in files]
        for i, f in enumerate(as_completed(futs), 1):
            status, key, extra = f.result()
            if status == "ok":
                ok += 1
                done_bytes += extra
            elif status == "skip":
                skip += 1
                done_bytes += extra
            else:
                err += 1
            with _print_lock:
                tag = {"ok": "DL ", "skip": "skip", "err": "ERR "}[status]
                short = key[len(BASE):] if key.startswith(BASE) else key
                line = f"[{i}/{len(files)}] {tag} {short}"
                if status == "err":
                    line += f"  -> {extra}"
                print(line)

    print(f"\nDone. downloaded={ok} skipped={skip} errors={err} "
          f"({human(done_bytes)} present locally under {args.dest})")


if __name__ == "__main__":
    main()

"""
distribute.py  -  Split downloaded photos among freelancers into per-person zips.

A "photo" = the image + its matching .psd, counted as ONE (same rule as the
downloader). Photos are dealt off the top of the pool in order: the first
freelancer's count, then the next's, and so on. Leftovers stay in the pool for
next time. Every photo handed out is recorded in a ledger so it is never issued
twice.

Usage (run from the MokshaImage folder):

    # preview a split, create nothing:
    python distribute.py catchall_ireland Vansh=12 Anish=8 --dry-run

    # actually build the zips + record the handout:
    python distribute.py catchall_ireland Vansh=12 Anish=8

    # how many photos are still unassigned for a brand:
    python distribute.py catchall_ireland --available

This module also exposes scan_available() and assign() so the web form can call
the same logic.
"""

import argparse
import datetime as dt
import json
import os
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
POOL = os.path.join(HERE, "pool")
ZIPS = os.path.join(HERE, "zips")
LEDGER = os.path.join(POOL, "_ledger.json")
HANDOUTS = os.path.join(POOL, "_handouts.json")  # audit log of deliveries

IMG_EXTS = (".psd", ".jpeg", ".jpg", ".png", ".tif", ".tiff")


# --- photo identity (mirrors download_s3.photo_id, kept local to avoid --------
#     importing the boto3-heavy downloader module) -----------------------------
def photo_id(rel_path):
    """Base identity shared by a photo's files. The .psd and its _orig .jpg/.jpeg
    collapse to the same id, so they count as one photo."""
    rel = rel_path.replace("\\", "/")
    folder, _, name = rel.rpartition("/")
    low = name.lower()
    for ext in IMG_EXTS:
        if low.endswith(ext):
            name = name[: -len(ext)]
            low = low[: -len(ext)]
            break
    if low.endswith("_orig"):
        name = name[: -len("_orig")]
    return f"{folder}/{name}"


def load_ledger():
    if os.path.exists(LEDGER):
        with open(LEDGER, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def load_handouts():
    """The delivery audit log: a list of records, one per freelancer delivery."""
    if os.path.exists(HANDOUTS):
        with open(HANDOUTS, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def append_handout(record):
    """Append one delivery record (date, brand, freelancer, photos, zip, link)."""
    hs = load_handouts()
    hs.append(record)
    os.makedirs(POOL, exist_ok=True)
    tmp = HANDOUTS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(hs, f, indent=2)
    os.replace(tmp, HANDOUTS)


def save_ledger(ledger):
    os.makedirs(POOL, exist_ok=True)
    tmp = LEDGER + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(ledger, f, indent=2)
    os.replace(tmp, LEDGER)


def scan_pool_photos(brand):
    """Return {photo_id: [absolute file paths]} for everything in pool/<brand>/."""
    base = os.path.join(POOL, brand)
    groups = {}
    if not os.path.isdir(base):
        return groups
    for root, _, files in os.walk(base):
        for fn in files:
            full = os.path.join(root, fn)
            rel = os.path.relpath(full, POOL)
            groups.setdefault(photo_id(rel), []).append(full)
    return groups


def scan_available(brand):
    """Photo ids in the pool for this brand that have NOT been handed out yet,
    sorted deterministically. Returns (available_ids, groups, ledger)."""
    groups = scan_pool_photos(brand)
    ledger = load_ledger()
    available = sorted(pid for pid in groups if pid not in ledger)
    return available, groups, ledger


def _zip_name(brand, freelancer, stamp):
    safe = "".join(c for c in freelancer if c.isalnum() or c in "-_") or "freelancer"
    return f"{brand}_{safe}_{stamp}.zip"


def assign(brand, splits, dry_run=False, commit=True):
    """Deal photos to freelancers in order and build one zip each.

    splits: list of (freelancer_name, count).
    dry_run: plan only, build nothing, touch nothing.
    commit:  when True (default, used by the CLI) the ledger is written here.
             The web form passes commit=False so it can upload each zip FIRST
             and only mark photos as handed out (via commit_results) once the
             upload has actually succeeded - so a failed upload never consumes
             photos from the pool.
    Returns a summary dict; each result carries its photo_ids for later commit.
    """
    available, groups, ledger = scan_available(brand)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")

    plan = []          # (name, count_requested, [photo_ids actually given])
    idx = 0
    for name, count in splits:
        take = available[idx: idx + count]
        idx += count
        plan.append((name, count, take))
    leftover = available[idx:]
    requested = sum(c for _, c in splits)
    shortfall = max(0, requested - len(available))

    results = []
    if not dry_run:
        os.makedirs(ZIPS, exist_ok=True)
    for name, count, pids in plan:
        files = [f for pid in pids for f in groups[pid]]
        zname = _zip_name(brand, name, stamp) if pids else None
        zpath = os.path.join(ZIPS, zname) if zname else None
        if not dry_run and pids:
            with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
                for f in files:
                    z.write(f, arcname=os.path.relpath(f, POOL))
        results.append({
            "freelancer": name,
            "requested": count,
            "given": len(pids),
            "files": len(files),
            "photo_ids": list(pids),
            "zip_name": zname,                       # prospective/actual name
            "zip": zpath if (not dry_run and pids) else None,  # real file only if written
            "stamp": stamp,
            "brand": brand,
        })

    if not dry_run and commit:
        commit_results(brand, results, ledger=ledger)

    return {
        "brand": brand,
        "available_before": len(available),
        "results": results,
        "leftover": len(leftover),
        "shortfall": shortfall,
        "dry_run": dry_run,
    }


def purge_files(paths):
    """Delete these local files and prune any folders left empty (up to POOL).
    Used after a delivery is safely in Drive, so the operator's disk never
    accumulates handed-out photos. The ledger keeps the record, so nothing is
    re-offered or re-downloaded."""
    removed = 0
    touched_dirs = set()
    for f in paths:
        try:
            os.remove(f)
            removed += 1
            touched_dirs.add(os.path.dirname(f))
        except OSError:
            pass
    pool_norm = os.path.normpath(POOL)
    for d in sorted(touched_dirs, key=len, reverse=True):
        p = d
        while os.path.normpath(p) != pool_norm and os.path.isdir(p):
            try:
                os.rmdir(p)          # removes only if empty
            except OSError:
                break
            p = os.path.dirname(p)
    return removed


def build_zip(brand, freelancer, photo_ids, stamp=None, groups=None):
    """Build one freelancer's zip from a list of photo_ids. Returns (name, path).
    Does not touch the ledger."""
    if groups is None:
        groups = scan_pool_photos(brand)
    stamp = stamp or dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    os.makedirs(ZIPS, exist_ok=True)
    zname = _zip_name(brand, freelancer, stamp)
    zpath = os.path.join(ZIPS, zname)
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for pid in photo_ids:
            for f in groups.get(pid, []):
                z.write(f, arcname=os.path.relpath(f, POOL))
    return zname, zpath


def commit_results(brand, results, ledger=None):
    """Mark the photos in these results as handed out. Call this only for
    results whose delivery actually succeeded (e.g. uploaded to Drive)."""
    if ledger is None:
        ledger = load_ledger()
    changed = False
    for r in results:
        for pid in r.get("photo_ids", []):
            ledger[pid] = {
                "freelancer": r["freelancer"],
                "brand": brand,
                "zip": r.get("zip_name"),
                "assigned_at": r.get("stamp"),
            }
            changed = True
    if changed:
        save_ledger(ledger)


def _parse_split(token):
    if "=" not in token:
        raise argparse.ArgumentTypeError(
            f"'{token}' must look like Name=Count, e.g. Vansh=35")
    name, _, num = token.partition("=")
    name = name.strip()
    if not name or not num.strip().isdigit():
        raise argparse.ArgumentTypeError(f"bad split '{token}'")
    return (name, int(num))


def main():
    ap = argparse.ArgumentParser(description="Split pooled photos into "
                                             "per-freelancer zips.")
    ap.add_argument("brand", help="brand folder name in the pool")
    ap.add_argument("splits", nargs="*", type=_parse_split,
                    help="one or more Name=Count, e.g. Vansh=35 Anish=40")
    ap.add_argument("--available", action="store_true",
                    help="just report how many photos are unassigned and exit")
    ap.add_argument("--dry-run", action="store_true",
                    help="show the plan, build nothing")
    args = ap.parse_args()

    if args.available:
        available, groups, ledger = scan_available(args.brand)
        total = len(groups)
        print(f"{args.brand}: {total} photos in pool, "
              f"{len(available)} unassigned, {total - len(available)} already given")
        return

    if not args.splits:
        ap.error("give at least one Name=Count (or use --available)")

    summary = assign(args.brand, args.splits, dry_run=args.dry_run)
    tag = "PLAN (dry run)" if summary["dry_run"] else "DONE"
    print(f"[{tag}] brand={summary['brand']} "
          f"unassigned available={summary['available_before']}")
    for r in summary["results"]:
        got = f"{r['given']}/{r['requested']}"
        short = "  <-- fewer than asked!" if r["given"] < r["requested"] else ""
        z = f"  -> {r['zip_name']}" if r.get("zip_name") else "  (no zip)"
        print(f"   {r['freelancer']:<15} photos {got:<8} files {r['files']:<4}{z}{short}")
    print(f"   leftover in pool: {summary['leftover']}")
    if summary["shortfall"]:
        print(f"   ! short by {summary['shortfall']} photos "
              f"(asked for more than available)")


if __name__ == "__main__":
    main()

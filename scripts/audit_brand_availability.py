"""READ-ONLY audit of one brand's "N available" count, for an authorised
operator. It explains where the number comes from, using the app's own rules
(hub.s3source), and compares it with the other sources of truth.

What it touches:
  * S3: list_objects_v2 only (no downloads, no writes). Credentials come from
    the normal AWS chain (env vars / ~/.aws profile) and are never printed.
  * Database (only with --db): one SELECT, in a read-only transaction. The URL
    is read from the DATABASE_URL environment variable, never the command line.
  * --ledger: the old desktop tool's pool/_ledger.json (read-only).
  * --pending: a text file of what the SLA sheet counts as pending, one
    S3 key / photo id / file name per line.
Output is aggregate counts and folder names only - no file names or ids.

    python scripts/audit_brand_availability.py catchall_ireland
    python scripts/audit_brand_availability.py catchall_ireland --db \
        --ledger D:/MokshaImage/pool/_ledger.json --pending pending.txt
"""

import argparse
import collections
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hub import s3source as src  # noqa: E402  (the app's own id/date rules)

OLD_LISTING_CAP = 1000      # main's date_folders made one unpaginated call


def _pid(key_or_id, base):
    return src.local_pid(src._photo_id(key_or_id if key_or_id.startswith(base)
                                       else base + key_or_id))


def _name(pid):
    return pid.rsplit("/", 1)[-1].lower()


def top_folders(s3, bucket, base, brand):
    names = []
    for page in s3.get_paginator("list_objects_v2").paginate(
            Bucket=bucket, Prefix=f"{base}{brand}/", Delimiter="/"):
        names += [p["Prefix"].split("/")[-2]
                  for p in page.get("CommonPrefixes", [])]
    return names


def scan_folder(s3, bucket, base, brand, folder):
    root = f"{base}{brand}/{folder}/"
    ids, psd, orig = set(), set(), set()
    exts, depth = collections.Counter(), collections.Counter()
    files = other = 0
    for page in s3.get_paginator("list_objects_v2").paginate(
            Bucket=bucket, Prefix=root):
        for o in page.get("Contents", []):
            k = o["Key"]
            if k.endswith("/"):
                continue
            files += 1
            low = k.lower()
            ext = os.path.splitext(low)[1] or "(none)"
            exts[ext] += 1
            if not low.endswith(src.IMG_EXTS):
                other += 1
                continue
            pid = src.local_pid(src._photo_id(k))
            ids.add(pid)
            depth[k[len(root):].count("/")] += 1
            (psd if low.endswith(".psd") else
             orig if os.path.splitext(low)[0].endswith("_orig") else set()).add(pid)
    lowered = collections.Counter(p.lower() for p in ids)
    variants = sum(1 for p in ids
                   if "_" in _name(p) and p.rsplit("_", 1)[0] in ids)
    return ids, {
        "files": files, "non_image_files": other, "photo_ids": len(ids),
        "ids_with_psd_and_orig": len(psd & orig),
        "ids_psd_only": len(psd - orig), "ids_orig_only": len(orig - psd),
        "ids_with_neither": len(ids - psd - orig),
        "ids_differing_only_by_case": sum(n for n in lowered.values() if n > 1),
        "ids_that_extend_another_id_with_underscore": variants,
        "image_files_by_subfolder_depth": dict(sorted(depth.items())),
        "files_by_extension": dict(sorted(exts.items())),
    }


def read_claims(db_url, brand):
    from sqlalchemy import create_engine, text
    for p in ("postgres://", "postgresql://"):
        if db_url.startswith(p):
            db_url = "postgresql+psycopg2://" + db_url[len(p):]
    kw = ({"connect_args": {"options": "-c default_transaction_read_only=on"}}
          if db_url.startswith("postgresql") else {})
    eng = create_engine(db_url, **kw)
    try:
        with eng.connect() as c:
            return c.execute(text(
                "SELECT ap.photo_id, ap.status FROM assignment_photos ap "
                "JOIN assignments a ON a.id = ap.assignment_id "
                "WHERE a.brand = :b"), {"b": brand}).all()
    finally:
        eng.dispose()


def audit(s3, brand, bucket=src.BUCKET, base=src.BASE, db_url=None,
          ledger_path=None, pending_path=None):
    names = top_folders(s3, bucket, base, brand)
    valid = sorted((n for n in names if src._is_date(n)), reverse=True)
    new_win = valid[:src.MAX_DATES]
    old_win = sorted(names[:OLD_LISTING_CAP], reverse=True)[:src.MAX_DATES]

    per, ids_in = {}, {}
    for f in sorted(set(new_win) | set(old_win), reverse=True):
        ids_in[f], per[f] = scan_folder(s3, bucket, base, brand, f)
    new_ids = set().union(*(ids_in[f] for f in new_win))
    old_ids = set().union(*(ids_in[f] for f in old_win))
    finished = set()
    for f in new_win:
        done = src.finished_names(brand, f)
        finished |= {p for p in ids_in[f] if p.rsplit("/", 1)[-1] in done}
        per[f]["finished_jpgs_matched"] = len(
            {p for p in ids_in[f] if p.rsplit("/", 1)[-1] in done})
    pending_ids = new_ids - finished
    out = {
        "brand": brand,
        "top_level_folders": {
            "total": len(names), "valid_date_folders": len(valid),
            "not_date_folders": sorted(n for n in names if not src._is_date(n)),
            "newest_valid": valid[:12], "oldest_valid": valid[-3:],
        },
        "window_now": new_win,
        "window_before_fix": old_win,
        "folders": per,
        "photo_ids_in_window_now": len(new_ids),
        "finished_in_window_now": len(finished),
        "pending_in_window_now": len(pending_ids),
    }

    claimed = set()
    if db_url:
        rows = read_claims(db_url, brand)
        claimed = {pid for pid, _ in rows}
        per_id = collections.Counter(pid for pid, _ in rows)
        in_old = [pid for pid, _ in rows
                  if any(pid.startswith(f"{brand}/{d}/") for d in old_win)]
        out["hub_claims"] = {
            "rows": len(rows), "distinct_photos": len(claimed),
            "photos_in_more_than_one_batch": sum(1 for n in per_id.values() if n > 1),
            "status_of_rows": dict(collections.Counter(st for _, st in rows)),
            "distinct_in_window_now": len(claimed & new_ids),
            "claimed_in_window_but_gone_from_s3": len(
                {p for p in claimed if any(p.startswith(f"{brand}/{d}/")
                                           for d in new_win)} - new_ids),
        }
        out["dropdown_now"] = len(pending_ids - claimed)
        out["dropdown_before_fix"] = max(0, len(old_ids) - len(in_old))

    remaining = pending_ids - claimed
    if ledger_path:
        with open(ledger_path, encoding="utf-8") as f:
            ledger = {k for k in json.load(f) if k.startswith(brand + "/")}
        out["legacy_ledger"] = {
            "brand_entries": len(ledger),
            "in_window_now": len(ledger & new_ids),
            "in_window_and_not_claimed_by_hub": len(ledger & remaining),
        }
        remaining -= ledger
        out["available_after_excluding_legacy"] = len(remaining)

    if pending_path:
        with open(pending_path, encoding="utf-8") as f:
            lines = [ln.strip() for ln in f if ln.strip()]
        by_name = collections.defaultdict(set)
        for p in new_ids:
            by_name[_name(p)].add(p)
        exact, named, ambiguous, missing = set(), set(), 0, 0
        for ln in lines:
            if "/" in ln:
                p = _pid(ln, base)
                if p in new_ids:
                    exact.add(p)
                else:
                    missing += 1
            else:
                hits = by_name.get(_name(_pid(ln, "")), set())
                if len(hits) == 1:
                    named |= hits
                else:
                    ambiguous += len(hits) > 1
                    missing += not hits
        matched = exact | named
        out["sla_pending"] = {
            "lines": len(lines), "matched_in_window": len(matched),
            "ambiguous_names": ambiguous, "not_in_window": missing,
            "pending_and_still_available": len(matched & remaining),
            "available_but_not_pending": len(remaining - matched),
        }
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("brand")
    ap.add_argument("--bucket", default=src.BUCKET)
    ap.add_argument("--prefix", default=src.BASE)
    ap.add_argument("--region", default=src.REGION)
    ap.add_argument("--db", action="store_true",
                    help="also read hub claims (URL from $DATABASE_URL)")
    ap.add_argument("--ledger", help="desktop tool's pool/_ledger.json")
    ap.add_argument("--pending", help="text file: SLA pending media, one per line")
    a = ap.parse_args(argv)
    db_url = None
    if a.db:
        db_url = os.environ.get("DATABASE_URL")
        if not db_url:
            sys.exit("--db needs DATABASE_URL in the environment")
    import boto3
    s3 = boto3.client("s3", region_name=a.region)
    print(json.dumps(audit(s3, a.brand, a.bucket, a.prefix, db_url,
                           a.ledger, a.pending), indent=2))


if __name__ == "__main__":
    main()

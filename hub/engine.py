"""The ported engine: everything the old JSON ledger did, now against the DB.

  create_assignment  - reserve N specific, unclaimed photos for a worker
  mark_downloaded    - worker pulled these from S3
  mark_uploaded      - finished files came back (matched by identical filename)
  assignment_progress / worker_summary / admin_overview - verification & views
"""

import datetime as dt
import re

from sqlalchemy import select, func

from .db import SessionLocal
from .models import (Worker, Assignment, AssignmentPhoto,
                     ST_ASSIGNED, ST_DOWNLOADED, ST_UPLOADED, ROLE_WORKER,
                     QC_OK, QC_REJECT, QC_RECTIFIED)
from . import s3source


# ---- people --------------------------------------------------------------
def get_or_create_worker(session, name, login=None, role=ROLE_WORKER):
    login = (login or name).strip().lower()
    w = session.scalar(select(Worker).where(Worker.login == login))
    if not w:
        w = Worker(name=name.strip(), login=login, role=role)
        session.add(w)
        session.flush()
    return w


def claimed_photo_ids(session, brand):
    """Every photo already reserved by any assignment for this brand - so two
    workers never get the same photo."""
    q = (select(AssignmentPhoto.photo_id)
         .join(Assignment)
         .where(Assignment.brand == brand))
    return set(session.scalars(q))


# ---- assigning -----------------------------------------------------------
def create_assignment(worker_login, brand, count, qc_login=None):
    """Reserve up to `count` unclaimed photos of `brand` for a worker (looked up
    by login; auto-created as a plain worker if they don't exist yet). Optionally
    assign a QC to review the batch."""
    with SessionLocal() as s:
        login = worker_login.strip().lower()
        w = s.scalar(select(Worker).where(Worker.login == login))
        if not w:
            w = Worker(name=worker_login.strip().title(), login=login,
                       role=ROLE_WORKER)
            s.add(w)
            s.flush()
        qc_id = None
        if qc_login:
            qc = s.scalar(select(Worker).where(Worker.login == qc_login.strip().lower()))
            qc_id = qc.id if qc else None
        taken = claimed_photo_ids(s, brand)
        pids = s3source.available_photo_ids(brand, taken, want=count)

        a = Assignment(worker_id=w.id, qc_id=qc_id, brand=brand, count=count)
        s.add(a)
        s.flush()
        for pid in pids:
            s.add(AssignmentPhoto(assignment_id=a.id, photo_id=pid,
                                  status=ST_ASSIGNED))
        s.commit()
        return {"assignment_id": a.id, "worker": w.name, "brand": brand,
                "requested": count, "reserved": len(pids),
                "short": max(0, count - len(pids)),
                "photo_ids": pids}


def assign_qc(assignment_id, qc_login):
    """Set (or clear, if qc_login is empty) the QC on an existing assignment."""
    with SessionLocal() as s:
        a = s.get(Assignment, assignment_id)
        if not a:
            return None
        if qc_login:
            qc = s.scalar(select(Worker).where(Worker.login == qc_login.strip().lower()))
            a.qc_id = qc.id if qc else None
        else:
            a.qc_id = None
        s.commit()
        return {"assignment_id": a.id, "qc": a.qc.name if a.qc else None}


# ---- progress transitions ------------------------------------------------
def _flip(session, assignment_id, photo_ids, new_status, drive_links=None):
    rows = session.scalars(
        select(AssignmentPhoto)
        .where(AssignmentPhoto.assignment_id == assignment_id)
        .where(AssignmentPhoto.photo_id.in_(list(photo_ids)))).all()
    changed = 0
    for r in rows:
        r.status = new_status
        if new_status == ST_UPLOADED:
            now = dt.datetime.utcnow()
            if r.first_uploaded_at is None:
                r.first_uploaded_at = now       # original submission time
            else:
                r.reuploaded_at = now           # this is a redo
            r.uploaded_at = now                 # always the latest
            if drive_links:
                r.drive_link = drive_links.get(r.photo_id, r.drive_link)
            # a (re)upload clears the live verdict so it re-enters review, but
            # reject_count/qc_by are left intact — they're the historical record
            r.qc = None
            r.qc_remark = ""
            r.qc_shot = ""
        changed += 1
    return changed


def mark_downloaded(assignment_id, photo_ids):
    with SessionLocal() as s:
        n = _flip(s, assignment_id, photo_ids, ST_DOWNLOADED)
        s.commit()
        return n


def mark_uploaded(assignment_id, photo_ids, drive_links=None):
    """Finished files came back. Because names match the originals, each
    uploaded file's id equals an assigned photo's id."""
    with SessionLocal() as s:
        n = _flip(s, assignment_id, photo_ids, ST_UPLOADED, drive_links)
        s.commit()
        return n


# ---- verification / views ------------------------------------------------
def _breakdown(photos):
    c = {ST_ASSIGNED: 0, ST_DOWNLOADED: 0, ST_UPLOADED: 0}
    for p in photos:
        c[p.status] = c.get(p.status, 0) + 1
    return c


def _last_upload(photos):
    """When the most recent finished file came in (i.e. when a completed batch
    was finished). None if nothing uploaded yet."""
    times = [p.uploaded_at for p in photos if p.uploaded_at]
    return max(times).strftime("%Y-%m-%d %H:%M") if times else None


def assignment_progress(assignment_id):
    with SessionLocal() as s:
        a = s.get(Assignment, assignment_id)
        if not a:
            return None
        photos = a.photos
        c = _breakdown(photos)
        return {
            "assignment_id": a.id, "worker": a.worker.name, "brand": a.brand,
            "when": a.created_at.strftime("%Y-%m-%d %H:%M"),
            "uploaded_when": _last_upload(photos),
            "assigned": len(photos),
            "to_download": c[ST_ASSIGNED],       # not pulled yet
            "downloaded": c[ST_DOWNLOADED],      # pulled, being retouched
            "uploaded": c[ST_UPLOADED],          # finished
            "remaining": len(photos) - c[ST_UPLOADED],
            "remaining_ids": [p.photo_id for p in photos
                              if p.status != ST_UPLOADED],
            "complete": c[ST_UPLOADED] == len(photos) and len(photos) > 0,
            "rejected": sum(1 for p in photos if p.qc == QC_REJECT),
            "rejected_list": [
                {"name": p.photo_id.split("/")[-1], "remark": p.qc_remark or "",
                 "shot": p.qc_shot or "", "shot_thumb": _drive_thumb(p.qc_shot)}
                for p in photos if p.qc == QC_REJECT],
        }


def _drive_thumb(link):
    """Turn a Drive share link into an inline thumbnail URL so images can be
    previewed on the page."""
    if not link:
        return None
    m = re.search(r"/d/([^/]+)", link)
    return f"https://drive.google.com/thumbnail?id={m.group(1)}&sz=w600" if m else None


def assignment_detail(assignment_id):
    with SessionLocal() as s:
        a = s.get(Assignment, assignment_id)
        if not a:
            return None
        return {
            "assignment_id": a.id, "worker": a.worker.name, "brand": a.brand,
            "qc": a.qc.name if a.qc else None,
            "qc_login": a.qc.login if a.qc else None,
            "when": a.created_at.strftime("%Y-%m-%d %H:%M"),
            "photos": [{"id": p.id, "name": p.photo_id.split("/")[-1],
                        "status": p.status, "link": p.drive_link,
                        "thumb": _drive_thumb(p.drive_link),
                        "qc": p.qc, "qc_remark": p.qc_remark or "",
                        "qc_shot": p.qc_shot or "",
                        "shot_thumb": _drive_thumb(p.qc_shot)}
                       for p in a.photos],
        }


# ---- QC verdicts ---------------------------------------------------------
def _qc_photo(s, photo_row_id, qc_login):
    """Fetch a photo only if the given QC is the one assigned to its batch."""
    p = s.get(AssignmentPhoto, photo_row_id)
    if not p:
        return None
    a = p.assignment
    if not a.qc or a.qc.login != qc_login.strip().lower():
        return None
    return p


def photo_qc_login(photo_row_id):
    """The login of the QC assigned to a photo's batch (or None)."""
    with SessionLocal() as s:
        p = s.get(AssignmentPhoto, photo_row_id)
        return p.assignment.qc.login if (p and p.assignment.qc) else None


def qc_mark_ok(photo_row_id, qc_login):
    with SessionLocal() as s:
        p = _qc_photo(s, photo_row_id, qc_login)
        if not p:
            return None
        p.qc = QC_OK
        p.qc_remark = p.qc_shot = ""
        p.qc_at = dt.datetime.utcnow()
        p.qc_by_id = p.assignment.qc_id      # who actually reviewed it
        aid = p.assignment_id
        s.commit()
        return aid


def qc_mark_reject(photo_row_id, qc_login, remark, shot_link=""):
    """Reject: record feedback and send the photo back to the worker to redo."""
    with SessionLocal() as s:
        p = _qc_photo(s, photo_row_id, qc_login)
        if not p:
            return None
        p.qc = QC_REJECT
        p.qc_remark = remark or ""
        p.qc_shot = shot_link or ""
        p.qc_at = dt.datetime.utcnow()
        p.qc_by_id = p.assignment.qc_id
        p.reject_count = (p.reject_count or 0) + 1   # counts toward error rate
        p.status = ST_DOWNLOADED     # back into the worker's "to redo" list
        aid = p.assignment_id
        s.commit()
        return aid


def qc_mark_rectify(photo_row_id, qc_login, new_link):
    """QC fixed it themselves: replace the file and mark rectified (final)."""
    with SessionLocal() as s:
        p = _qc_photo(s, photo_row_id, qc_login)
        if not p:
            return None
        p.qc = QC_RECTIFIED
        p.drive_link = new_link
        p.qc_remark = p.qc_shot = ""
        p.qc_at = dt.datetime.utcnow()
        p.qc_by_id = p.assignment.qc_id
        aid = p.assignment_id
        s.commit()
        return aid


def _qc_counts(photos):
    """QC breakdown for a set of photos."""
    ok = sum(1 for p in photos if p.qc == QC_OK)
    rect = sum(1 for p in photos if p.qc == QC_RECTIFIED)
    rej = sum(1 for p in photos if p.qc == QC_REJECT)
    pending = sum(1 for p in photos
                  if p.status == ST_UPLOADED and p.qc is None)
    return {"ok": ok, "rectified": rect, "rejected": rej, "pending": pending}


def _stage(photos):
    """Which of the two steps a batch is at, for the admin."""
    total = len(photos)
    if total == 0:
        return "empty"
    q = _qc_counts(photos)
    done = q["ok"] + q["rectified"]
    if done == total:
        return "complete"                      # step 2 finished
    uploaded = sum(1 for p in photos if p.status == ST_UPLOADED)
    if uploaded == total and q["pending"] == 0 and q["rejected"] == 0:
        return "complete"
    if uploaded == total or q["pending"] or q["rejected"] or done:
        return "qc"                            # worker done, QC underway
    return "worker"                            # step 1 still in progress


def worker_summary(worker_login):
    with SessionLocal() as s:
        w = s.scalar(select(Worker).where(Worker.login == worker_login.lower()))
        if not w:
            return None
        ordered = sorted(w.assignments, key=lambda x: x.created_at, reverse=True)
        return {"worker": w.name,
                "assignments": [assignment_progress(a.id) for a in ordered]}


_UP_EXTS = (".psd", ".jpeg", ".jpg", ".png", ".tif", ".tiff")


def _name_key(name):
    """The comparable base of a file/photo: drop folders, extension and the
    _orig suffix. 'abc-v4_orig.jpeg' and 'abc-v4.psd' both -> 'abc-v4'."""
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    low = name.lower()
    for ext in _UP_EXTS:
        if low.endswith(ext):
            name = name[: -len(ext)]
            low = low[: -len(ext)]
            break
    if low.endswith("_orig"):
        name = name[: -len("_orig")]
    return name


def match_uploads(assignment_id, filenames):
    """Match uploaded filenames to this assignment's not-yet-finished photos by
    base name. Returns (matched {filename: photo_id}, unmatched [filename]).
    Files that don't match any assigned photo are flagged as unmatched; assigned
    photos with no matching upload simply stay 'not done' (the missing ones)."""
    with SessionLocal() as s:
        a = s.get(Assignment, assignment_id)
        if not a:
            return {}, list(filenames)
        keymap = {_name_key(p.photo_id): p.photo_id
                  for p in a.photos if p.status != ST_UPLOADED}
        matched, unmatched = {}, []
        for fn in filenames:
            pid = keymap.get(_name_key(fn))
            (matched.__setitem__(fn, pid) if pid else unmatched.append(fn))
        return matched, unmatched


def worker_assignments(login):
    """Full detail (photo ids + statuses) of a worker's assignments — used by
    the companion app over the API."""
    with SessionLocal() as s:
        w = s.scalar(select(Worker).where(Worker.login == login.strip().lower()))
        if not w:
            return []
        return [{
            "assignment_id": a.id, "brand": a.brand, "count": a.count,
            "photos": [{"photo_id": p.photo_id, "status": p.status}
                       for p in a.photos],
        } for a in w.assignments]


def assignment_owner_login(assignment_id):
    with SessionLocal() as s:
        a = s.get(Assignment, assignment_id)
        return a.worker.login if a else None


def assignment_photo_ids(assignment_id, status=None):
    with SessionLocal() as s:
        a = s.get(Assignment, assignment_id)
        if not a:
            return []
        return [p.photo_id for p in a.photos
                if status is None or p.status == status]


def admin_overview():
    """One row per assignment for the admin dashboard."""
    with SessionLocal() as s:
        rows = []
        for a in s.scalars(select(Assignment).order_by(Assignment.created_at.desc())):
            c = _breakdown(a.photos)
            total = len(a.photos)
            qc = _qc_counts(a.photos)
            rows.append({
                "assignment_id": a.id, "worker": a.worker.name,
                "qc": a.qc.name if a.qc else None,
                "brand": a.brand, "assigned": total,
                "uploaded": c[ST_UPLOADED], "downloaded": c[ST_DOWNLOADED],
                "to_download": c[ST_ASSIGNED],
                "remaining": total - c[ST_UPLOADED],
                "when": a.created_at.strftime("%Y-%m-%d %H:%M"),
                "uploaded_when": _last_upload(a.photos),
                "qc_ok": qc["ok"], "qc_rectified": qc["rectified"],
                "qc_rejected": qc["rejected"], "qc_pending": qc["pending"],
                "qc_done": qc["ok"] + qc["rectified"], "stage": _stage(a.photos),
            })
        return rows


def totals():
    """Overall QC metrics across every assignment, for the admin summary."""
    with SessionLocal() as s:
        photos = s.query(AssignmentPhoto).all()
        return {
            "done": sum(1 for p in photos if p.qc == QC_OK),
            "rectified": sum(1 for p in photos if p.qc == QC_RECTIFIED),
            "rejected": sum(1 for p in photos if p.qc == QC_REJECT),
        }


def qc_assignments(qc_login):
    """The batches a QC has been assigned to review, with the worker's live
    status. Newest first."""
    with SessionLocal() as s:
        qc = s.scalar(select(Worker).where(Worker.login == qc_login.strip().lower()))
        if not qc:
            return []
        rows = []
        q = (select(Assignment).where(Assignment.qc_id == qc.id)
             .order_by(Assignment.created_at.desc()))
        for a in s.scalars(q):
            c = _breakdown(a.photos)
            total = len(a.photos)
            qc = _qc_counts(a.photos)
            rows.append({
                "assignment_id": a.id, "worker": a.worker.name,
                "brand": a.brand, "assigned": total,
                "uploaded": c[ST_UPLOADED], "downloaded": c[ST_DOWNLOADED],
                "to_download": c[ST_ASSIGNED],
                "remaining": total - c[ST_UPLOADED],
                "complete": c[ST_UPLOADED] == total and total > 0,
                "when": a.created_at.strftime("%Y-%m-%d %H:%M"),
                "uploaded_when": _last_upload(a.photos),
                "qc_pending": qc["pending"], "qc_ok": qc["ok"],
                "qc_rectified": qc["rectified"], "qc_rejected": qc["rejected"],
                "qc_done": qc["ok"] + qc["rectified"],
            })
        return rows


# ---- reporting -----------------------------------------------------------
def _fmt(t):
    return t.strftime("%Y-%m-%d %H:%M") if t else ""


def _verdict_label(p):
    """A human reading of where a photo stands, for the report."""
    if p.qc == QC_OK:
        return "Approved"
    if p.qc == QC_RECTIFIED:
        return "Rectified by QC"
    if p.qc == QC_REJECT:
        return "Rejected — redo"
    if p.status == ST_UPLOADED:
        return f"Pending QC (redone ×{p.reject_count})" if p.reject_count \
            else "Pending QC"
    if p.status == ST_DOWNLOADED:
        return "Redoing" if p.reject_count else "Retouching"
    return "Not downloaded"


def _photo_row(a, p):
    """One image's full lifecycle + QC record, as a flat dict for a report row."""
    if p.qc_by:                         # the QC who actually made the verdict
        by_name, by_login = p.qc_by.name, p.qc_by.login
    elif a.qc:                          # assigned but hasn't acted yet
        by_name, by_login = a.qc.name, a.qc.login
    else:
        by_name, by_login = "", ""
    return {
        "worker": a.worker.name, "worker_login": a.worker.login,
        "brand": a.brand, "assignment_id": a.id,
        "file": p.photo_id.split("/")[-1], "photo_id": p.photo_id,
        "assigned_at": _fmt(a.created_at),
        "uploaded_at": _fmt(p.first_uploaded_at),
        "reuploaded_at": _fmt(p.reuploaded_at),
        "status": p.status, "verdict": _verdict_label(p), "qc_code": p.qc,
        "reject_count": p.reject_count or 0,
        "uploaded_ever": p.first_uploaded_at is not None,
        "qc_remark": p.qc_remark or "", "qc_shot": p.qc_shot or "",
        "qc_at": _fmt(p.qc_at), "qc_by": by_name, "qc_by_login": by_login,
    }


def _worker_stats(rows):
    total = len(rows)
    uploaded = sum(1 for r in rows if r["uploaded_ever"])
    rejected = sum(1 for r in rows if r["reject_count"] > 0)   # ever needed redo
    approved = sum(1 for r in rows if r["qc_code"] == QC_OK)
    rectified = sum(1 for r in rows if r["qc_code"] == QC_RECTIFIED)
    pending = sum(1 for r in rows
                  if r["qc_code"] is None and r["status"] == ST_UPLOADED)
    err = round(rejected / uploaded * 100, 1) if uploaded else 0.0
    return {"total": total, "uploaded": uploaded, "rejected": rejected,
            "approved": approved, "rectified": rectified, "pending": pending,
            "error_rate": err}


def report_data(start, end, worker_login=None):
    """Every photo whose batch was allotted in [start, end) (end exclusive),
    grouped by worker, with per-image timestamps, QC verdict + assessor, and a
    per-worker error rate. Optionally narrowed to a single worker."""
    with SessionLocal() as s:
        q = (select(Assignment)
             .where(Assignment.created_at >= start)
             .where(Assignment.created_at < end)
             .order_by(Assignment.created_at))
        if worker_login:
            w = s.scalar(select(Worker)
                         .where(Worker.login == worker_login.strip().lower()))
            if not w:
                return {"start_label": start.strftime("%Y-%m-%d"),
                        "end_label": (end - dt.timedelta(days=1)).strftime("%Y-%m-%d"),
                        "workers": [], "master": []}
            q = q.where(Assignment.worker_id == w.id)

        by_worker = {}
        master = []
        for a in s.scalars(q):
            for p in sorted(a.photos, key=lambda x: x.photo_id):
                row = _photo_row(a, p)
                master.append(row)
                wk = by_worker.setdefault(
                    a.worker_id,
                    {"name": a.worker.name, "login": a.worker.login, "rows": []})
                wk["rows"].append(row)

        workers = []
        for wk in by_worker.values():
            workers.append({**wk, **_worker_stats(wk["rows"])})
        workers.sort(key=lambda x: x["name"].lower())

        return {
            "start_label": start.strftime("%Y-%m-%d"),
            "end_label": (end - dt.timedelta(days=1)).strftime("%Y-%m-%d"),
            "generated": dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M"),
            "workers": workers, "master": master,
        }

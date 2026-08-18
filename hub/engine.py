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
from .models import (Worker, Assignment, AssignmentPhoto, QcReview, Notification,
                     ST_ASSIGNED, ST_DOWNLOADED, ST_UPLOADED, ROLE_WORKER,
                     ROLE_ADMIN, QC_OK, QC_REJECT, QC_RECTIFIED)
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
# The live verdicts (QC_OK / QC_RECTIFIED) are now applied in bulk by
# submit_qc_batch (see the batch-QC section below), not per photo.
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
    # in the batch-QC model an "error" is a photo the QC had to correct
    corrected = sum(1 for r in rows if r["qc_code"] == QC_RECTIFIED)
    approved = sum(1 for r in rows if r["qc_code"] == QC_OK)
    rejected = sum(1 for r in rows if r["reject_count"] > 0)   # legacy redo data
    pending = sum(1 for r in rows
                  if r["qc_code"] is None and r["status"] == ST_UPLOADED)
    err = round(corrected / uploaded * 100, 1) if uploaded else 0.0
    return {"total": total, "uploaded": uploaded, "corrected": corrected,
            "approved": approved, "rectified": corrected, "rejected": rejected,
            "pending": pending, "error_rate": err}


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


# ---- batch QC (download all -> upload total + corrected) -----------------
def file_id_from_link(link):
    """Pull the Drive file id out of a share link (…/d/<id>/…)."""
    if not link:
        return None
    m = re.search(r"/d/([^/]+)", link)
    return m.group(1) if m else None


def _qc_owns(a, qc_login):
    return bool(a and a.qc and a.qc.login == qc_login.strip().lower())


def qc_batch_view(assignment_id):
    """Everything the QC batch page needs: worker/brand, upload progress, the
    worker's finished photos, and the last review (if any)."""
    with SessionLocal() as s:
        a = s.get(Assignment, assignment_id)
        if not a:
            return None
        photos = sorted(a.photos, key=lambda x: x.photo_id)
        uploaded = [p for p in photos if p.status == ST_UPLOADED]
        review = s.scalars(
            select(QcReview).where(QcReview.assignment_id == a.id)
            .order_by(QcReview.created_at.desc())).first()
        return {
            "assignment_id": a.id, "worker": a.worker.name, "brand": a.brand,
            "qc_login": a.qc.login if a.qc else None,
            "total": len(photos), "uploaded": len(uploaded),
            "all_uploaded": bool(photos) and len(uploaded) == len(photos),
            "photos": [{"name": p.photo_id.split("/")[-1], "link": p.drive_link,
                        "thumb": _drive_thumb(p.drive_link), "qc": p.qc}
                       for p in uploaded],
            "review": ({"total": review.total, "corrected": review.corrected,
                        "rate": round(review.corrected / review.total * 100, 1)
                        if review.total else 0.0,
                        "note": review.note,
                        "when": review.created_at.strftime("%Y-%m-%d %H:%M")}
                       if review else None),
        }


def qc_batch_download(assignment_id, qc_login):
    """The Drive files (id + fallback name) of the worker's finished photos, for
    the QC's bulk-download zip. None if this QC doesn't own the batch."""
    with SessionLocal() as s:
        a = s.get(Assignment, assignment_id)
        if not _qc_owns(a, qc_login):
            return None
        files = []
        for p in sorted(a.photos, key=lambda x: x.photo_id):
            fid = file_id_from_link(p.drive_link)
            if fid:
                files.append({"file_id": fid, "name": p.photo_id.split("/")[-1]})
        return {"brand": a.brand, "files": files}


def submit_qc_batch(assignment_id, qc_login, total_links, corrected_names,
                    note=""):
    """Apply a whole-batch QC review.

      total_links      {filename: new_drive_link} for the QC's re-uploaded set;
                       each replaces the worker's file for the matched photo.
      corrected_names  the filenames the QC flagged as corrected (a subset).
      note             a short message shown to the worker.

    Matched photos are marked Approved (or Corrected by QC if flagged), the
    review is recorded, and the worker + admins are notified. Returns the counts
    plus the worker's now-stale Drive links to clean up, or None if this QC
    doesn't own the batch."""
    with SessionLocal() as s:
        a = s.get(Assignment, assignment_id)
        if not _qc_owns(a, qc_login):
            return None
        photos = a.photos
        keymap = {_name_key(p.photo_id): p for p in photos}
        corrected_keys = {_name_key(fn) for fn in corrected_names}
        now = dt.datetime.utcnow()
        old_links, touched = [], set()

        for fname, link in total_links.items():
            p = keymap.get(_name_key(fname))
            if not p:
                continue
            if p.drive_link and p.drive_link != link:
                old_links.append(p.drive_link)      # worker's version, now stale
            p.drive_link = link
            p.status = ST_UPLOADED
            if p.first_uploaded_at is None:
                p.first_uploaded_at = now
            p.qc = QC_OK
            p.qc_remark = p.qc_shot = ""
            p.qc_at = now
            p.qc_by_id = a.qc_id
            touched.add(p.id)

        for p in photos:                            # flag the corrected subset
            if p.id in touched and _name_key(p.photo_id) in corrected_keys:
                p.qc = QC_RECTIFIED

        total = sum(1 for p in photos if p.status == ST_UPLOADED)
        corrected = sum(1 for p in photos if p.qc == QC_RECTIFIED)
        rate = round(corrected / total * 100, 1) if total else 0.0

        s.add(QcReview(assignment_id=a.id, qc_id=a.qc_id, total=total,
                       corrected=corrected, note=note or ""))

        worker_name, brand, qc_name = a.worker.name, a.brand, a.qc.name
        wmsg = (f"QC checked your “{brand}” batch: {total} reviewed, "
                f"{corrected} corrected ({rate}% error rate). Please make sure "
                f"not to repeat the mistakes found in those {corrected}.")
        if note:
            wmsg += f"  QC note: “{note}”"
        s.add(Notification(recipient_id=a.worker_id, message=wmsg,
                           assignment_id=a.id))
        amsg = (f"{qc_name} reviewed {worker_name}’s “{brand}”: {total} checked, "
                f"{corrected} corrected — {rate}% error rate.")
        for admin in s.scalars(select(Worker).where(Worker.role == ROLE_ADMIN)):
            s.add(Notification(recipient_id=admin.id, message=amsg,
                               assignment_id=a.id))
        s.commit()
        return {"total": total, "corrected": corrected, "rate": rate,
                "old_links": old_links, "worker": worker_name, "brand": brand}


def worker_corrections(login, limit=24):
    """Recent photos a QC corrected in this worker's batches, newest first, so
    the worker can see and learn from them."""
    with SessionLocal() as s:
        w = s.scalar(select(Worker).where(Worker.login == login.strip().lower()))
        if not w:
            return []
        out = []
        for a in sorted(w.assignments, key=lambda x: x.created_at, reverse=True):
            for p in a.photos:
                if p.qc == QC_RECTIFIED:
                    out.append({
                        "brand": a.brand, "name": p.photo_id.split("/")[-1],
                        "link": p.drive_link, "thumb": _drive_thumb(p.drive_link),
                        "when": p.qc_at.strftime("%Y-%m-%d %H:%M")
                        if p.qc_at else ""})
        return out[:limit]


# ---- notifications -------------------------------------------------------
def unread_notifications(login):
    with SessionLocal() as s:
        w = s.scalar(select(Worker).where(Worker.login == login.strip().lower()))
        if not w:
            return []
        q = (select(Notification)
             .where(Notification.recipient_id == w.id)
             .where(Notification.is_read.is_(False))
             .order_by(Notification.created_at.desc()))
        return [{"id": n.id, "message": n.message,
                 "assignment_id": n.assignment_id,
                 "when": n.created_at.strftime("%Y-%m-%d %H:%M")}
                for n in s.scalars(q)]


def mark_notification_read(nid, login):
    with SessionLocal() as s:
        w = s.scalar(select(Worker).where(Worker.login == login.strip().lower()))
        if not w:
            return False
        n = s.get(Notification, nid)
        if not n or n.recipient_id != w.id:
            return False
        n.is_read = True
        s.commit()
        return True

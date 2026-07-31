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
                     ST_ASSIGNED, ST_DOWNLOADED, ST_UPLOADED, ROLE_WORKER)
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
            r.uploaded_at = dt.datetime.utcnow()
            if drive_links:
                r.drive_link = drive_links.get(r.photo_id, r.drive_link)
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
            "photos": [{"name": p.photo_id.split("/")[-1], "status": p.status,
                        "link": p.drive_link, "thumb": _drive_thumb(p.drive_link)}
                       for p in a.photos],
        }


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
            rows.append({
                "assignment_id": a.id, "worker": a.worker.name,
                "qc": a.qc.name if a.qc else None,
                "brand": a.brand, "assigned": total,
                "uploaded": c[ST_UPLOADED], "downloaded": c[ST_DOWNLOADED],
                "to_download": c[ST_ASSIGNED],
                "remaining": total - c[ST_UPLOADED],
                "when": a.created_at.strftime("%Y-%m-%d %H:%M"),
                "uploaded_when": _last_upload(a.photos),
            })
        return rows


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
            rows.append({
                "assignment_id": a.id, "worker": a.worker.name,
                "brand": a.brand, "assigned": total,
                "uploaded": c[ST_UPLOADED], "downloaded": c[ST_DOWNLOADED],
                "to_download": c[ST_ASSIGNED],
                "remaining": total - c[ST_UPLOADED],
                "complete": c[ST_UPLOADED] == total and total > 0,
                "when": a.created_at.strftime("%Y-%m-%d %H:%M"),
                "uploaded_when": _last_upload(a.photos),
            })
        return rows

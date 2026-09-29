"""The whole state of the platform in three tables.

  workers            - people who log in (admins, retouchers, QC)
  assignments        - "worker owes N photos of brand X"
  assignment_photos  - one row per assigned photo, tracked through its life
                       (assigned -> downloaded -> uploaded). Verification is
                       just counting these rows by status.
"""

import datetime as dt

from sqlalchemy import String, Integer, ForeignKey, DateTime, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base

ROLE_ADMIN = "admin"
ROLE_WORKER = "worker"
ROLE_QC = "qc"

# lifecycle of a single assigned photo (the worker pipeline)
ST_ASSIGNED = "assigned"       # reserved for the worker, not yet pulled
ST_DOWNLOADED = "downloaded"   # worker fetched it from S3 (also where a rejected
                               # photo returns to, for redo)
ST_UPLOADED = "uploaded"       # finished file came back to Drive

# the QC verdict layered on top of an uploaded photo
QC_OK = "ok"                   # approved as-is
QC_REJECT = "reject"           # sent back to the worker to redo
QC_RECTIFIED = "rectified"     # QC fixed it themselves and re-uploaded


class Worker(Base):
    __tablename__ = "workers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    login: Mapped[str] = mapped_column(String(200), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255), default="")
    role: Mapped[str] = mapped_column(String(20), default=ROLE_WORKER)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=dt.datetime.utcnow)
    # the admin who added this person. Each admin's portal only shows their own
    # people and those people's batches, so separate teams never get mixed up.
    # Empty for the first admin.
    owner_id: Mapped[int | None] = mapped_column(
        ForeignKey("workers.id"), nullable=True)
    # set when an admin deletes the person: they can't sign in and drop out of
    # every list, but the row stays so their finished work keeps its name
    removed_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime, nullable=True)

    assignments: Mapped[list["Assignment"]] = relationship(
        back_populates="worker", cascade="all, delete-orphan",
        foreign_keys="Assignment.worker_id")


class Assignment(Base):
    __tablename__ = "assignments"

    id: Mapped[int] = mapped_column(primary_key=True)
    worker_id: Mapped[int] = mapped_column(ForeignKey("workers.id"))
    qc_id: Mapped[int | None] = mapped_column(
        ForeignKey("workers.id"), nullable=True)   # QC reviewing this batch
    brand: Mapped[str] = mapped_column(String(200))
    count: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=dt.datetime.utcnow)

    worker: Mapped["Worker"] = relationship(
        back_populates="assignments", foreign_keys=[worker_id])
    qc: Mapped["Worker | None"] = relationship(foreign_keys=[qc_id])
    photos: Mapped[list["AssignmentPhoto"]] = relationship(
        back_populates="assignment", cascade="all, delete-orphan")


class AssignmentPhoto(Base):
    __tablename__ = "assignment_photos"

    id: Mapped[int] = mapped_column(primary_key=True)
    assignment_id: Mapped[int] = mapped_column(ForeignKey("assignments.id"))
    # the pool-relative unique base id, e.g.
    # catchall_ireland/20260716/exterior-1/.../<name>-v4
    photo_id: Mapped[str] = mapped_column(Text, index=True)
    status: Mapped[str] = mapped_column(String(20), default=ST_ASSIGNED)
    drive_link: Mapped[str] = mapped_column(Text, default="")
    uploaded_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime, nullable=True)                             # most recent upload
    # kept separately so a redo doesn't erase the original submission time
    first_uploaded_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime, nullable=True)                             # first submission
    reuploaded_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime, nullable=True)                             # latest redo, if any

    # QC layer
    qc: Mapped[str | None] = mapped_column(String(20), nullable=True)
    qc_remark: Mapped[str] = mapped_column(Text, default="")
    qc_shot: Mapped[str] = mapped_column(Text, default="")   # screenshot link
    qc_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    # how many times this photo was rejected & sent back (for the error rate);
    # survives the redo loop, unlike `qc` which is cleared on re-upload
    reject_count: Mapped[int] = mapped_column(Integer, default=0)
    # the QC who actually made the current verdict — stamped per photo because
    # a worker's reviewer can change day to day
    qc_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("workers.id"), nullable=True)

    assignment: Mapped["Assignment"] = relationship(back_populates="photos")
    qc_by: Mapped["Worker | None"] = relationship(foreign_keys=[qc_by_id])


class QcReview(Base):
    """One whole-batch QC review: what the QC reported for an assignment — how
    many they checked, how many they had to correct, and their note to the
    worker. corrected / total is the batch error rate."""
    __tablename__ = "qc_reviews"

    id: Mapped[int] = mapped_column(primary_key=True)
    assignment_id: Mapped[int] = mapped_column(ForeignKey("assignments.id"))
    qc_id: Mapped[int | None] = mapped_column(
        ForeignKey("workers.id"), nullable=True)
    total: Mapped[int] = mapped_column(Integer, default=0)
    corrected: Mapped[int] = mapped_column(Integer, default=0)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=dt.datetime.utcnow)

    assignment: Mapped["Assignment"] = relationship()
    qc: Mapped["Worker | None"] = relationship(foreign_keys=[qc_id])


class Notification(Base):
    """A one-line in-app message for a user (worker or admin), shown as a banner
    on their dashboard until they dismiss it."""
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    recipient_id: Mapped[int] = mapped_column(ForeignKey("workers.id"))
    message: Mapped[str] = mapped_column(Text)
    assignment_id: Mapped[int | None] = mapped_column(
        ForeignKey("assignments.id"), nullable=True)
    is_read: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=dt.datetime.utcnow)

    recipient: Mapped["Worker"] = relationship(foreign_keys=[recipient_id])

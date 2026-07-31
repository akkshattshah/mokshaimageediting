"""The whole state of the platform in three tables.

  workers            - people who log in (one admin, many retouchers)
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

# lifecycle of a single assigned photo
ST_ASSIGNED = "assigned"       # reserved for the worker, not yet pulled
ST_DOWNLOADED = "downloaded"   # worker fetched it from S3
ST_UPLOADED = "uploaded"       # finished file came back to Drive -> done


class Worker(Base):
    __tablename__ = "workers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    login: Mapped[str] = mapped_column(String(200), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255), default="")
    role: Mapped[str] = mapped_column(String(20), default=ROLE_WORKER)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=dt.datetime.utcnow)

    assignments: Mapped[list["Assignment"]] = relationship(
        back_populates="worker", cascade="all, delete-orphan")


class Assignment(Base):
    __tablename__ = "assignments"

    id: Mapped[int] = mapped_column(primary_key=True)
    worker_id: Mapped[int] = mapped_column(ForeignKey("workers.id"))
    brand: Mapped[str] = mapped_column(String(200))
    count: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=dt.datetime.utcnow)

    worker: Mapped["Worker"] = relationship(back_populates="assignments")
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
        DateTime, nullable=True)

    assignment: Mapped["Assignment"] = relationship(back_populates="photos")

"""Accounts, passwords, and roles. Passwords are hashed (never stored plain)
via werkzeug, which ships with Flask.

Every account except the first admin has an owner - the admin who added it. An
admin's portal only shows their own people, so several admins can each run a
team without their work getting mixed up. Logins are still unique platform-wide
(there's one sign-in page)."""

import os

from sqlalchemy import select, update
from werkzeug.security import generate_password_hash, check_password_hash

from .db import SessionLocal
from .models import Worker, ROLE_ADMIN, ROLE_WORKER


def _info(w):
    return {"id": w.id, "name": w.name, "login": w.login, "role": w.role,
            "owner_id": w.owner_id}


def create_user(name, login, password, role=ROLE_WORKER, owner_id=None):
    """Create a user, or update their name/role/password if the login exists."""
    login_l = login.strip().lower()
    with SessionLocal() as s:
        w = s.scalar(select(Worker).where(Worker.login == login_l))
        updated = w is not None
        if not w:
            w = Worker(login=login_l, owner_id=owner_id)
            s.add(w)
        w.name = name.strip()
        w.role = role
        w.removed_at = None
        if password:
            w.password_hash = generate_password_hash(password)
        s.commit()
        return {**_info(w), "updated": updated}


def add_person(admin_id, name, login, password, role=ROLE_WORKER):
    """Add someone to an admin's team, or update / bring back one of their own
    people under the same login. A login that belongs to anyone else is refused
    (returns taken=True). An existing admin keeps the admin role - delete them
    to take it away - so nobody can lock themselves or another admin out."""
    login_l = login.strip().lower()
    with SessionLocal() as s:
        w = s.scalar(select(Worker).where(Worker.login == login_l))
        if w and admin_id not in (w.id, w.owner_id):
            return {"taken": True, "login": login_l}
        restored = bool(w and w.removed_at)
        updated = w is not None and not restored
        if not w:
            w = Worker(login=login_l, owner_id=admin_id)
            s.add(w)
        w.name = name.strip()
        if restored or w.role != ROLE_ADMIN:
            w.role = role
        w.removed_at = None
        w.password_hash = generate_password_hash(password)
        s.commit()
        return {**_info(w), "taken": False, "updated": updated,
                "restored": restored}


def authenticate(login, password):
    with SessionLocal() as s:
        w = s.scalar(select(Worker).where(Worker.login == login.strip().lower()))
        if (w and w.removed_at is None and w.password_hash
                and check_password_hash(w.password_hash, password)):
            return _info(w)
        return None


def get_user(uid):
    """The live account behind a session - None once they've been deleted."""
    with SessionLocal() as s:
        w = s.get(Worker, uid)
        return _info(w) if w and w.removed_at is None else None


def list_workers(role=None, owner_id=None):
    """Active (not deleted) accounts - optionally only one role, and/or only
    one admin's team."""
    with SessionLocal() as s:
        q = (select(Worker).where(Worker.removed_at.is_(None))
             .order_by(Worker.role, Worker.name))
        if role:
            q = q.where(Worker.role == role)
        if owner_id is not None:
            q = q.where(Worker.owner_id == owner_id)
        return [_info(w) for w in s.scalars(q)]


def seed_admin():
    """Guarantee one admin exists so the platform is usable on first run.
    Uses ADMIN_LOGIN / ADMIN_PASSWORD from the environment, else safe defaults
    (which should be changed after first login)."""
    with SessionLocal() as s:
        if s.scalar(select(Worker).where(Worker.role == ROLE_ADMIN,
                                         Worker.removed_at.is_(None))):
            return None
    login = os.environ.get("ADMIN_LOGIN", "admin")
    pw = os.environ.get("ADMIN_PASSWORD", "admin123")
    return create_user("Admin", login, pw, ROLE_ADMIN)


def adopt_unowned():
    """Accounts made before admins had separate portals have no owner: give
    them to the first admin, whose portal they've always been in. Safe to run
    on every startup. Returns how many were adopted."""
    with SessionLocal() as s:
        first = s.scalar(select(Worker)
                         .where(Worker.role == ROLE_ADMIN,
                                Worker.removed_at.is_(None))
                         .order_by(Worker.id))
        if not first:
            return 0
        res = s.execute(update(Worker)
                        .where(Worker.owner_id.is_(None), Worker.id != first.id)
                        .values(owner_id=first.id))
        s.commit()
        return res.rowcount

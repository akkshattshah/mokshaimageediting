"""Accounts, passwords, and roles. Passwords are hashed (never stored plain)
via werkzeug, which ships with Flask."""

import os

from sqlalchemy import select
from werkzeug.security import generate_password_hash, check_password_hash

from .db import SessionLocal
from .models import Worker, ROLE_ADMIN, ROLE_WORKER


def create_user(name, login, password, role=ROLE_WORKER):
    """Create a user, or update their name/role/password if the login exists."""
    login_l = login.strip().lower()
    with SessionLocal() as s:
        w = s.scalar(select(Worker).where(Worker.login == login_l))
        updated = w is not None
        if not w:
            w = Worker(login=login_l)
            s.add(w)
        w.name = name.strip()
        w.role = role
        if password:
            w.password_hash = generate_password_hash(password)
        s.commit()
        return {"id": w.id, "name": w.name, "login": w.login,
                "role": w.role, "updated": updated}


def authenticate(login, password):
    with SessionLocal() as s:
        w = s.scalar(select(Worker).where(Worker.login == login.strip().lower()))
        if w and w.password_hash and check_password_hash(w.password_hash, password):
            return {"id": w.id, "name": w.name, "login": w.login, "role": w.role}
        return None


def list_workers(role=None):
    with SessionLocal() as s:
        q = select(Worker).order_by(Worker.role, Worker.name)
        if role:
            q = q.where(Worker.role == role)
        return [{"id": w.id, "name": w.name, "login": w.login, "role": w.role}
                for w in s.scalars(q)]


def seed_admin():
    """Guarantee one admin exists so the platform is usable on first run.
    Uses ADMIN_LOGIN / ADMIN_PASSWORD from the environment, else safe defaults
    (which should be changed after first login)."""
    with SessionLocal() as s:
        if s.scalar(select(Worker).where(Worker.role == ROLE_ADMIN)):
            return None
    login = os.environ.get("ADMIN_LOGIN", "admin")
    pw = os.environ.get("ADMIN_PASSWORD", "admin123")
    return create_user("Admin", login, pw, ROLE_ADMIN)

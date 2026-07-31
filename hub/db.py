"""Database setup. Uses Railway's Postgres in production (via DATABASE_URL) and
falls back to a local SQLite file for development/testing - the same code runs
on both."""

import os

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker, DeclarativeBase

DB_URL = os.environ.get("DATABASE_URL", "sqlite:///hub_dev.db")
# Railway/Heroku hand out "postgres://"; SQLAlchemy wants "postgresql://".
if DB_URL.startswith("postgres://"):
    DB_URL = DB_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DB_URL, future=True, echo=False, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


class Base(DeclarativeBase):
    pass


def _add_missing_columns():
    """Lightweight migration: add columns that were introduced after a table
    already existed in production (create_all only creates missing TABLES, not
    missing COLUMNS). Safe to run every startup."""
    insp = inspect(engine)
    if "assignments" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("assignments")}
    if "qc_id" not in cols:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE assignments ADD COLUMN qc_id INTEGER"))


def init_db():
    """Create tables if they don't exist yet, then patch in any new columns."""
    from . import models  # noqa: F401  (registers the tables on Base)
    Base.metadata.create_all(engine)
    _add_missing_columns()

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
    """Lightweight migration: add columns introduced after a table already
    existed in production (create_all only creates missing TABLES, not missing
    COLUMNS). Safe to run every startup."""
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    wanted = {
        "assignments": {"qc_id": "INTEGER"},
        "assignment_photos": {
            "qc": "TEXT", "qc_remark": "TEXT", "qc_shot": "TEXT",
            "qc_at": "TIMESTAMP",
        },
    }
    for table, cols in wanted.items():
        if table not in tables:
            continue
        existing = {c["name"] for c in insp.get_columns(table)}
        for col, coltype in cols.items():
            if col not in existing:
                with engine.begin() as conn:
                    conn.execute(text(
                        f"ALTER TABLE {table} ADD COLUMN {col} {coltype}"))


def init_db():
    """Create tables if they don't exist yet, then patch in any new columns."""
    from . import models  # noqa: F401  (registers the tables on Base)
    Base.metadata.create_all(engine)
    _add_missing_columns()

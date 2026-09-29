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
        "workers": {"owner_id": "INTEGER", "removed_at": "TIMESTAMP"},
        "assignments": {"qc_id": "INTEGER"},
        "assignment_photos": {
            "qc": "TEXT", "qc_remark": "TEXT", "qc_shot": "TEXT",
            "qc_at": "TIMESTAMP", "first_uploaded_at": "TIMESTAMP",
            "reuploaded_at": "TIMESTAMP", "reject_count": "INTEGER",
            "qc_by_id": "INTEGER",
        },
    }
    for table, cols in wanted.items():
        if table not in tables:
            continue
        existing = {c["name"] for c in insp.get_columns(table)}
        for col, coltype in cols.items():
            if col not in existing:
                try:
                    with engine.begin() as conn:
                        conn.execute(text(
                            f"ALTER TABLE {table} ADD COLUMN {col} {coltype}"))
                except Exception:
                    # the other server process may have added it at the same
                    # moment - that's fine; anything else is a real error
                    fresh = {c["name"] for c in inspect(engine).get_columns(table)}
                    if col not in fresh:
                        raise


def init_db():
    """Create tables if they don't exist yet, then patch in any new columns."""
    from . import models  # noqa: F401  (registers the tables on Base)
    Base.metadata.create_all(engine)
    _add_missing_columns()

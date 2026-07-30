"""Database setup. Uses Railway's Postgres in production (via DATABASE_URL) and
falls back to a local SQLite file for development/testing - the same code runs
on both."""

import os

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase

DB_URL = os.environ.get("DATABASE_URL", "sqlite:///hub_dev.db")
# Railway/Heroku hand out "postgres://"; SQLAlchemy wants "postgresql://".
if DB_URL.startswith("postgres://"):
    DB_URL = DB_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DB_URL, future=True, echo=False, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


class Base(DeclarativeBase):
    pass


def init_db():
    """Create tables if they don't exist yet."""
    from . import models  # noqa: F401  (registers the tables on Base)
    Base.metadata.create_all(engine)

"""Database session and initialization helpers."""

from __future__ import annotations

from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from coherence_engine.server.fund.config import settings


class Base(DeclarativeBase):
    """Base declarative model."""


engine = create_engine(
    settings.DATABASE_URL,
    future=True,
    pool_pre_ping=True,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False, class_=Session)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency for DB session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Create all tables (starter migration path)."""
    from coherence_engine.server.fund import models  # noqa: F401

    Base.metadata.create_all(bind=engine)


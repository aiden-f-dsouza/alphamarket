from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from alphamarket.config import get_settings


class Base(DeclarativeBase):
    pass


def _make_engine():
    return create_engine(get_settings().sqlalchemy_url, pool_pre_ping=True)


engine = _make_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a DB session. Overridden in tests."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

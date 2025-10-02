"""Database initialization helpers."""
from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from .config import get_settings


class Base(DeclarativeBase):
    """Base class for SQLAlchemy models."""


def create_engine() -> AsyncEngine:
    """Create a configured SQLAlchemy async engine."""

    settings = get_settings()
    return create_async_engine(settings.database_url, echo=settings.echo_sql)


engine = create_engine()
SessionFactory = async_sessionmaker(bind=engine, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    """Yield an :class:`AsyncSession` for FastAPI dependencies."""

    async with SessionFactory() as session:
        yield session


__all__ = [
    "Base",
    "engine",
    "SessionFactory",
    "get_session",
]

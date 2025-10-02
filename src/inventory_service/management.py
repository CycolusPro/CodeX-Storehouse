"""Utility helpers for administrative tasks."""
from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import AsyncEngine

from .database import Base, engine


async def init_database(db_engine: AsyncEngine | None = None) -> None:
    """Create database tables for the application."""

    engine_to_use = db_engine or engine
    async with engine_to_use.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def cli_init_database() -> None:
    """CLI wrapper executed from :mod:`python -m`."""

    asyncio.run(init_database())


if __name__ == "__main__":
    cli_init_database()

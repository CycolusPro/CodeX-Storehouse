from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from inventory_service.api import create_app
from inventory_service.config import Settings
from inventory_service.database import Base, get_session


@pytest.fixture(scope="session")
def event_loop() -> AsyncIterator[asyncio.AbstractEventLoop]:
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture()
async def app(tmp_path) -> AsyncIterator[FastAPI]:
    db_path = tmp_path / "test.db"
    test_settings = Settings(
        database_url=f"sqlite+aiosqlite:///{db_path}",
        environment="test",
        access_control_allow_origin="*",
        app_name="Test Inventory Service",
    )

    engine = create_async_engine(test_settings.database_url, echo=False)
    async_session = async_sessionmaker(bind=engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async def override_get_session() -> AsyncIterator[AsyncSession]:
        async with async_session() as session:
            yield session

    app = create_app(test_settings)
    app.dependency_overrides[get_session] = override_get_session

    yield app

    await engine.dispose()


@pytest.fixture()
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(app=app, base_url="http://test") as client:
        yield client

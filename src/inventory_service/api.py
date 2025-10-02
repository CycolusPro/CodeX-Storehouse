"""FastAPI router configuration."""
from __future__ import annotations

from typing import Sequence

from fastapi import APIRouter, Depends, FastAPI, HTTPException, status
from sqlalchemy.exc import NoResultFound
from sqlalchemy.ext.asyncio import AsyncSession

from . import crud, schemas
from .config import Settings, get_settings
from .database import get_session

router = APIRouter()


def provide_settings() -> Settings:
    """Dependency returning the active :class:`Settings` instance."""

    return get_settings()


@router.get("/health", response_model=schemas.HealthStatus, tags=["system"])
async def health_check(settings: Settings = Depends(provide_settings)) -> schemas.HealthStatus:
    return schemas.HealthStatus(environment=settings.environment)


@router.post("/products", response_model=schemas.ProductOut, status_code=status.HTTP_201_CREATED)
async def create_product(
    payload: schemas.ProductCreate, session: AsyncSession = Depends(get_session)
) -> schemas.ProductOut:
    product = await crud.create_product(session, payload)
    await session.commit()
    return schemas.ProductOut.from_orm(product)


@router.get("/products", response_model=list[schemas.ProductOut])
async def list_products(session: AsyncSession = Depends(get_session)) -> Sequence[schemas.ProductOut]:
    products = await crud.list_products(session)
    return [schemas.ProductOut.from_orm(product) for product in products]


@router.get("/products/{product_id}", response_model=schemas.ProductOut)
async def get_product(
    product_id: int, session: AsyncSession = Depends(get_session)
) -> schemas.ProductOut:
    try:
        product = await crud.get_product(session, product_id)
    except NoResultFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return schemas.ProductOut.from_orm(product)


@router.put("/products/{product_id}", response_model=schemas.ProductOut)
async def update_product(
    product_id: int,
    payload: schemas.ProductUpdate,
    session: AsyncSession = Depends(get_session),
) -> schemas.ProductOut:
    try:
        product = await crud.get_product(session, product_id)
    except NoResultFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    product = await crud.update_product(session, product, payload)
    await session.commit()
    await session.refresh(product)
    return schemas.ProductOut.from_orm(product)


@router.delete("/products/{product_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_product(product_id: int, session: AsyncSession = Depends(get_session)) -> None:
    try:
        product = await crud.get_product(session, product_id)
    except NoResultFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    await crud.delete_product(session, product)
    await session.commit()


@router.post("/locations", response_model=schemas.LocationOut, status_code=status.HTTP_201_CREATED)
async def create_location(
    payload: schemas.LocationCreate, session: AsyncSession = Depends(get_session)
) -> schemas.LocationOut:
    location = await crud.create_location(session, payload)
    await session.commit()
    return schemas.LocationOut.from_orm(location)


@router.get("/locations", response_model=list[schemas.LocationOut])
async def list_locations(
    session: AsyncSession = Depends(get_session),
) -> Sequence[schemas.LocationOut]:
    locations = await crud.list_locations(session)
    return [schemas.LocationOut.from_orm(location) for location in locations]


@router.get("/locations/{location_id}", response_model=schemas.LocationOut)
async def get_location(
    location_id: int, session: AsyncSession = Depends(get_session)
) -> schemas.LocationOut:
    try:
        location = await crud.get_location(session, location_id)
    except NoResultFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return schemas.LocationOut.from_orm(location)


@router.put("/locations/{location_id}", response_model=schemas.LocationOut)
async def update_location(
    location_id: int,
    payload: schemas.LocationUpdate,
    session: AsyncSession = Depends(get_session),
) -> schemas.LocationOut:
    try:
        location = await crud.get_location(session, location_id)
    except NoResultFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    location = await crud.update_location(session, location, payload)
    await session.commit()
    await session.refresh(location)
    return schemas.LocationOut.from_orm(location)


@router.delete("/locations/{location_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_location(
    location_id: int, session: AsyncSession = Depends(get_session)
) -> None:
    try:
        location = await crud.get_location(session, location_id)
    except NoResultFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    await crud.delete_location(session, location)
    await session.commit()


@router.post(
    "/inventory/adjustments",
    response_model=schemas.InventoryMovementOut,
    status_code=status.HTTP_201_CREATED,
)
async def adjust_inventory(
    payload: schemas.InventoryAdjustment, session: AsyncSession = Depends(get_session)
) -> schemas.InventoryMovementOut:
    try:
        movement, balance = await crud.adjust_inventory(session, payload)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    await session.commit()
    await session.refresh(movement)
    await session.refresh(balance)
    return schemas.InventoryMovementOut.from_orm(movement)


@router.get("/inventory/balances", response_model=list[schemas.InventoryBalanceOut])
async def list_balances(
    session: AsyncSession = Depends(get_session),
) -> Sequence[schemas.InventoryBalanceOut]:
    return await crud.list_inventory_balances(session)


@router.get("/inventory/movements", response_model=list[schemas.InventoryMovementOut])
async def list_movements(
    session: AsyncSession = Depends(get_session),
) -> Sequence[schemas.InventoryMovementOut]:
    movements = await crud.list_movements(session)
    return [schemas.InventoryMovementOut.from_orm(movement) for movement in movements]


@router.get("/inventory/low-stock", response_model=list[schemas.LowStockItem])
async def list_low_stock(
    session: AsyncSession = Depends(get_session),
) -> Sequence[schemas.LowStockItem]:
    return await crud.list_low_stock_items(session)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title=settings.app_name)
    if settings is not None:
        app.dependency_overrides[provide_settings] = lambda: settings
    app.include_router(router)
    return app


app = create_app()


__all__ = ["app", "create_app"]

"""Business logic for interacting with the database."""
from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import Select, and_, select
from sqlalchemy.exc import NoResultFound
from sqlalchemy.ext.asyncio import AsyncSession

from . import schemas
from .models import InventoryBalance, InventoryMovement, Location, Product


async def create_product(session: AsyncSession, data: schemas.ProductCreate) -> Product:
    product = Product(**data.dict())
    session.add(product)
    await session.flush()
    return product


async def list_products(session: AsyncSession) -> Sequence[Product]:
    stmt = select(Product).order_by(Product.name)
    result = await session.execute(stmt)
    return result.scalars().all()


async def get_product(session: AsyncSession, product_id: int) -> Product:
    stmt = select(Product).where(Product.id == product_id)
    result = await session.execute(stmt)
    product = result.scalar_one_or_none()
    if product is None:
        raise NoResultFound(f"Product {product_id} not found")
    return product


async def update_product(
    session: AsyncSession, product: Product, data: schemas.ProductUpdate
) -> Product:
    for field, value in data.dict(exclude_unset=True).items():
        setattr(product, field, value)
    await session.flush()
    return product


async def delete_product(session: AsyncSession, product: Product) -> None:
    await session.delete(product)
    await session.flush()


async def create_location(session: AsyncSession, data: schemas.LocationCreate) -> Location:
    location = Location(**data.dict())
    session.add(location)
    await session.flush()
    return location


async def list_locations(session: AsyncSession) -> Sequence[Location]:
    stmt = select(Location).order_by(Location.name)
    result = await session.execute(stmt)
    return result.scalars().all()


async def get_location(session: AsyncSession, location_id: int) -> Location:
    stmt = select(Location).where(Location.id == location_id)
    result = await session.execute(stmt)
    location = result.scalar_one_or_none()
    if location is None:
        raise NoResultFound(f"Location {location_id} not found")
    return location


async def update_location(
    session: AsyncSession, location: Location, data: schemas.LocationUpdate
) -> Location:
    for field, value in data.dict(exclude_unset=True).items():
        setattr(location, field, value)
    await session.flush()
    return location


async def delete_location(session: AsyncSession, location: Location) -> None:
    await session.delete(location)
    await session.flush()


async def _get_balance(
    session: AsyncSession, *, product_id: int, location_id: int
) -> InventoryBalance:
    stmt = select(InventoryBalance).where(
        InventoryBalance.product_id == product_id,
        InventoryBalance.location_id == location_id,
    )
    result = await session.execute(stmt)
    balance = result.scalar_one_or_none()
    if balance is None:
        balance = InventoryBalance(
            product_id=product_id,
            location_id=location_id,
            quantity=0,
        )
        session.add(balance)
        await session.flush()
    return balance


async def adjust_inventory(
    session: AsyncSession, data: schemas.InventoryAdjustment
) -> tuple[InventoryMovement, InventoryBalance]:
    balance = await _get_balance(
        session, product_id=data.product_id, location_id=data.location_id
    )
    new_quantity = balance.quantity + data.quantity_change
    if new_quantity < 0:
        raise ValueError("Cannot reduce inventory below zero.")

    movement = InventoryMovement(**data.dict())
    session.add(movement)

    balance.quantity = new_quantity
    await session.flush()
    return movement, balance


async def list_inventory_balances(session: AsyncSession) -> Sequence[schemas.InventoryBalanceOut]:
    stmt = (
        select(
            InventoryBalance.product_id,
            Product.name.label("product_name"),
            InventoryBalance.location_id,
            Location.name.label("location_name"),
            InventoryBalance.quantity,
        )
        .join(Product, InventoryBalance.product_id == Product.id)
        .join(Location, InventoryBalance.location_id == Location.id)
        .order_by(Product.name, Location.name)
    )
    result = await session.execute(stmt)
    rows = result.all()
    return [
        schemas.InventoryBalanceOut(
            product_id=row.product_id,
            product_name=row.product_name,
            location_id=row.location_id,
            location_name=row.location_name,
            quantity=row.quantity,
        )
        for row in rows
    ]


async def list_movements(session: AsyncSession) -> Sequence[InventoryMovement]:
    stmt = select(InventoryMovement).order_by(InventoryMovement.performed_at.desc())
    result = await session.execute(stmt)
    return result.scalars().all()


async def list_low_stock_items(session: AsyncSession) -> Sequence[schemas.LowStockItem]:
    stmt: Select = (
        select(
            InventoryBalance.product_id,
            Product.name.label("product_name"),
            InventoryBalance.location_id,
            Location.name.label("location_name"),
            InventoryBalance.quantity,
            Product.reorder_point,
        )
        .join(Product, InventoryBalance.product_id == Product.id)
        .join(Location, InventoryBalance.location_id == Location.id)
        .where(
            and_(
                Product.reorder_point.is_not(None),
                Product.reorder_point > 0,
                InventoryBalance.quantity <= Product.reorder_point,
            )
        )
        .order_by(Product.name)
    )
    result = await session.execute(stmt)
    rows = result.all()
    return [
        schemas.LowStockItem(
            product_id=row.product_id,
            product_name=row.product_name,
            location_id=row.location_id,
            location_name=row.location_name,
            quantity=row.quantity,
            reorder_point=row.reorder_point,
        )
        for row in rows
    ]


__all__ = [name for name in globals() if not name.startswith("_")]

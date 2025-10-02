"""Pydantic schemas used by the API."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class ProductBase(BaseModel):
    sku: str = Field(..., description="Unique stock keeping unit identifier.")
    name: str
    description: str | None = None
    category: str | None = None
    unit: str = Field("unit", description="Unit of measurement, e.g. bag, cup, ml.")
    reorder_point: int = Field(0, ge=0)


class ProductCreate(ProductBase):
    pass


class ProductUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    category: str | None = None
    unit: str | None = None
    reorder_point: int | None = Field(default=None, ge=0)


class ProductOut(ProductBase):
    id: int
    created_at: datetime
    updated_at: datetime

    class Config:
        orm_mode = True


class LocationBase(BaseModel):
    name: str
    type: str | None = Field(None, description="Store, warehouse, kiosk, etc.")
    address: str | None = None
    contact: str | None = None


class LocationCreate(LocationBase):
    pass


class LocationUpdate(BaseModel):
    name: str | None = None
    type: str | None = None
    address: str | None = None
    contact: str | None = None


class LocationOut(LocationBase):
    id: int
    created_at: datetime
    updated_at: datetime

    class Config:
        orm_mode = True


class InventoryAdjustment(BaseModel):
    product_id: int
    location_id: int
    quantity_change: int = Field(..., description="Positive for stock-in, negative for stock-out.")
    reason: str | None = None
    reference: str | None = None
    note: str | None = None
    performed_by: str | None = None


class InventoryMovementOut(InventoryAdjustment):
    id: int
    performed_at: datetime

    class Config:
        orm_mode = True


class InventoryBalanceOut(BaseModel):
    product_id: int
    product_name: str
    location_id: int
    location_name: str
    quantity: int


class LowStockItem(BaseModel):
    product_id: int
    product_name: str
    location_id: int
    location_name: str
    quantity: int
    reorder_point: int


class HealthStatus(BaseModel):
    status: Literal["ok"] = "ok"
    environment: str


__all__ = [
    "ProductCreate",
    "ProductUpdate",
    "ProductOut",
    "LocationCreate",
    "LocationUpdate",
    "LocationOut",
    "InventoryAdjustment",
    "InventoryMovementOut",
    "InventoryBalanceOut",
    "LowStockItem",
    "HealthStatus",
]

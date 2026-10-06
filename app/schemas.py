"""Modelos de entrada/salida de la API (Pydantic v2)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from .domain import Reservation, ReservationStatus


class ReservationCreate(BaseModel):
    user_id: str = Field(min_length=1)
    store_id: str = Field(min_length=1)
    product_id: str = Field(min_length=1)
    quantity: int = Field(gt=0, description="Cantidad entera positiva")
    idempotency_key: str = Field(min_length=1)


class AvailabilityResponse(BaseModel):
    store_id: str
    product_id: str
    available: int


class ReservationResponse(BaseModel):
    reservation_id: str
    user_id: str
    store_id: str
    product_id: str
    quantity: int
    status: ReservationStatus
    created_at: datetime
    expires_at: datetime

    @classmethod
    def from_reservation(
        cls, reservation: Reservation, status: ReservationStatus
    ) -> "ReservationResponse":
        return cls(
            reservation_id=reservation.id,
            user_id=reservation.user_id,
            store_id=reservation.store_id,
            product_id=reservation.product_id,
            quantity=reservation.quantity,
            status=status,
            created_at=reservation.created_at,
            expires_at=reservation.expires_at,
        )


class ErrorResponse(BaseModel):
    error: str
    code: str

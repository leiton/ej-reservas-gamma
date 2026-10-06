"""Entidades de dominio y errores.

El estado almacenado de una reserva es solo ACTIVE o CONFIRMED. El estado
EXPIRED es *derivado* del tiempo: una reserva activa cuyo vencimiento ya pasó se
considera vencida sin necesidad de mutarla (vencimiento perezoso).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class ReservationStatus(str, Enum):
    ACTIVE = "active"
    CONFIRMED = "confirmed"
    EXPIRED = "expired"  # solo como estado efectivo/derivado, nunca persistido


@dataclass
class Reservation:
    id: str
    user_id: str
    store_id: str
    product_id: str
    quantity: int
    idempotency_key: str
    created_at: datetime
    expires_at: datetime
    # Estado persistido: ACTIVE o CONFIRMED. EXPIRED se calcula con effective_status.
    status: ReservationStatus = ReservationStatus.ACTIVE

    def effective_status(self, now: datetime) -> ReservationStatus:
        """Estado real teniendo en cuenta el tiempo.

        - CONFIRMED es terminal: una reserva confirmada nunca vence.
        - Una ACTIVE cuyo vencimiento ya pasó (now >= expires_at) está EXPIRED.
        """
        if self.status is ReservationStatus.CONFIRMED:
            return ReservationStatus.CONFIRMED
        if now >= self.expires_at:
            return ReservationStatus.EXPIRED
        return ReservationStatus.ACTIVE

    def matches(self, user_id: str, store_id: str, product_id: str, quantity: int) -> bool:
        """¿Los datos de este request coinciden con los de la reserva?

        Se usa para la idempotencia: misma clave + mismos datos == reintento.
        """
        return (
            self.user_id == user_id
            and self.store_id == store_id
            and self.product_id == product_id
            and self.quantity == quantity
        )


# --- Errores de dominio -----------------------------------------------------


class DomainError(Exception):
    """Base de los errores de negocio. Cada subclase mapea a un HTTP status."""

    code = "domain_error"


class ProductNotFound(DomainError):
    code = "product_not_found"


class InvalidQuantity(DomainError):
    code = "invalid_quantity"


class InsufficientStock(DomainError):
    code = "insufficient_stock"


class IdempotencyConflict(DomainError):
    code = "idempotency_conflict"


class ReservationNotFound(DomainError):
    code = "reservation_not_found"


class ReservationExpired(DomainError):
    code = "reservation_expired"

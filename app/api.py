"""Capa HTTP (FastAPI).

Los handlers son síncronos (`def`), de modo que FastAPI los ejecuta en su
threadpool: hay hilos reales del sistema y, por lo tanto, la garantía de
no-sobreventa que da el lock del store se ejercita de verdad bajo concurrencia.
"""

from __future__ import annotations

from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse

from .domain import (
    DomainError,
    IdempotencyConflict,
    InsufficientStock,
    InvalidQuantity,
    ProductNotFound,
    ReservationExpired,
    ReservationNotFound,
)
from .schemas import AvailabilityResponse, ReservationCreate, ReservationResponse
from .store import ReservationStore

# Mapeo de cada error de dominio a su código HTTP.
_STATUS_BY_EXCEPTION: dict[type[DomainError], int] = {
    ProductNotFound: 404,
    ReservationNotFound: 404,
    InsufficientStock: 409,
    IdempotencyConflict: 409,
    ReservationExpired: 409,
    InvalidQuantity: 422,
}


def create_app(store: ReservationStore) -> FastAPI:
    app = FastAPI(title="Reservas de stock - quick commerce", version="1.0.0")
    app.state.store = store

    def get_store() -> ReservationStore:
        return app.state.store

    @app.exception_handler(DomainError)
    async def _handle_domain_error(_: Request, exc: DomainError) -> JSONResponse:
        status = _STATUS_BY_EXCEPTION.get(type(exc), 400)
        return JSONResponse(
            status_code=status,
            content={"error": str(exc), "code": exc.code},
        )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/availability", response_model=AvailabilityResponse)
    def get_availability(
        store_id: str,
        product_id: str,
        store: ReservationStore = Depends(get_store),
    ) -> AvailabilityResponse:
        available = store.availability(store_id, product_id)
        return AvailabilityResponse(
            store_id=store_id, product_id=product_id, available=available
        )

    @app.post("/reservations", response_model=ReservationResponse)
    def create_reservation(
        payload: ReservationCreate,
        response: Response,
        store: ReservationStore = Depends(get_store),
    ) -> ReservationResponse:
        reservation, created = store.reserve(
            user_id=payload.user_id,
            store_id=payload.store_id,
            product_id=payload.product_id,
            quantity=payload.quantity,
            idempotency_key=payload.idempotency_key,
        )
        # 201 si se creó; 200 si fue un reintento idempotente.
        response.status_code = 201 if created else 200
        status = reservation.effective_status(store.current_time())
        return ReservationResponse.from_reservation(reservation, status)

    @app.post("/reservations/{reservation_id}/confirm", response_model=ReservationResponse)
    def confirm_reservation(
        reservation_id: str,
        store: ReservationStore = Depends(get_store),
    ) -> ReservationResponse:
        reservation = store.confirm(reservation_id)
        status = reservation.effective_status(store.current_time())
        return ReservationResponse.from_reservation(reservation, status)

    return app

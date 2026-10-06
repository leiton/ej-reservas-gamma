"""Almacén de reservas en memoria.

Modelo *derivado*: no se mantiene un contador mutable de "disponible". El total
físico por (tienda, producto) es fijo (sembrado al arrancar, porque reponer
inventario está fuera de alcance) y la disponibilidad se calcula restando:

    disponible = total - unidades_activas_no_vencidas - unidades_confirmadas

Ventajas: el vencimiento es automático (comparación de tiempo, sin job en
background) y confirmar solo reclasifica una reserva de "activa" a "confirmada"
sin volver a tocar inventario, por lo que no hay riesgo de doble descuento.

Toda operación que lee-modifica-escribe el estado compartido se serializa con un
único lock global: es la forma más obviamente correcta de garantizar que las
solicitudes concurrentes nunca reserven más unidades de las existentes. El
README explica cómo evolucionar a locks por (tienda, producto) para más
throughput.
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timedelta

from .clock import Clock
from .domain import (
    IdempotencyConflict,
    InsufficientStock,
    InvalidQuantity,
    ProductNotFound,
    Reservation,
    ReservationExpired,
    ReservationNotFound,
    ReservationStatus,
)

StockKey = tuple[str, str]  # (store_id, product_id)


class ReservationStore:
    def __init__(
        self,
        initial_stock: dict[StockKey, int],
        clock: Clock,
        ttl_seconds: int,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds debe ser positivo")
        self._totals: dict[StockKey, int] = dict(initial_stock)
        self._clock = clock
        self._ttl = timedelta(seconds=ttl_seconds)
        self._reservations: dict[str, Reservation] = {}
        self._idempotency: dict[str, str] = {}  # idempotency_key -> reservation_id
        self._lock = threading.Lock()

    # --- helpers (asumen que el lock ya está tomado) ------------------------

    def _available_locked(self, store_id: str, product_id: str, now: datetime) -> int:
        key: StockKey = (store_id, product_id)
        if key not in self._totals:
            raise ProductNotFound(f"No existe stock para {store_id}/{product_id}")
        total = self._totals[key]
        held = 0
        sold = 0
        for res in self._reservations.values():
            if res.store_id != store_id or res.product_id != product_id:
                continue
            status = res.effective_status(now)
            if status is ReservationStatus.ACTIVE:
                held += res.quantity
            elif status is ReservationStatus.CONFIRMED:
                sold += res.quantity
            # EXPIRED: sus unidades vuelven a estar disponibles -> no se restan
        return total - held - sold

    # --- API pública --------------------------------------------------------

    def current_time(self) -> datetime:
        return self._clock.now()

    def availability(self, store_id: str, product_id: str) -> int:
        with self._lock:
            now = self._clock.now()
            return self._available_locked(store_id, product_id, now)

    def reserve(
        self,
        *,
        user_id: str,
        store_id: str,
        product_id: str,
        quantity: int,
        idempotency_key: str,
    ) -> tuple[Reservation, bool]:
        """Crea una reserva.

        Devuelve (reserva, created). created=False indica un reintento idempotente
        que devolvió una reserva ya existente sin descontar nada nuevo.
        """
        if quantity <= 0:
            raise InvalidQuantity("La cantidad debe ser un entero positivo")

        with self._lock:
            now = self._clock.now()

            # 1. Idempotencia: la clave es única en todo el servicio.
            existing_id = self._idempotency.get(idempotency_key)
            if existing_id is not None:
                existing = self._reservations[existing_id]
                if not existing.matches(user_id, store_id, product_id, quantity):
                    # Misma clave, datos distintos -> conflicto.
                    raise IdempotencyConflict(
                        "La idempotency_key ya fue usada con datos diferentes"
                    )
                # Misma clave + mismos datos -> se devuelve la reserva existente
                # tal cual (aunque esté vencida o confirmada); nunca se crea otra
                # ni se vuelve a descontar.
                return existing, False

            # 2. Validar que el par (tienda, producto) exista y haya stock.
            available = self._available_locked(store_id, product_id, now)
            if available < quantity:
                # Rechazo por falta de stock: no se modifica inventario ni se
                # guarda la clave (los intentos rechazados no se persisten).
                raise InsufficientStock(
                    f"Stock insuficiente: disponible {available}, solicitado {quantity}"
                )

            # 3. Crear la reserva.
            reservation = Reservation(
                id=str(uuid.uuid4()),
                user_id=user_id,
                store_id=store_id,
                product_id=product_id,
                quantity=quantity,
                idempotency_key=idempotency_key,
                created_at=now,
                expires_at=now + self._ttl,
                status=ReservationStatus.ACTIVE,
            )
            self._reservations[reservation.id] = reservation
            self._idempotency[idempotency_key] = reservation.id
            return reservation, True

    def confirm(self, reservation_id: str) -> Reservation:
        with self._lock:
            now = self._clock.now()
            reservation = self._reservations.get(reservation_id)
            if reservation is None:
                raise ReservationNotFound(f"No existe la reserva {reservation_id}")

            status = reservation.effective_status(now)
            if status is ReservationStatus.CONFIRMED:
                # Confirmación repetida: éxito idempotente, sin tocar inventario.
                return reservation
            if status is ReservationStatus.EXPIRED:
                raise ReservationExpired(
                    f"La reserva {reservation_id} venció y no puede confirmarse"
                )

            # ACTIVE -> CONFIRMED. Las unidades reservadas pasan a vendidas; como
            # ambas restan de la disponibilidad, no hay doble descuento.
            reservation.status = ReservationStatus.CONFIRMED
            return reservation

    # Útil para tests/depuración.
    def get(self, reservation_id: str) -> Reservation | None:
        with self._lock:
            return self._reservations.get(reservation_id)

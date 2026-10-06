"""Tests del núcleo de dominio (ReservationStore).

Cubren las garantías que el ejercicio pide demostrar:
  - no-sobreventa bajo concurrencia
  - idempotencia de la creación (3 casos)
  - confirmación repetida (idempotente, sin doble descuento)
  - vencimiento perezoso y recuperación de stock
  - interacción entre confirmación y vencimiento
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from app.clock import FakeClock
from app.domain import (
    IdempotencyConflict,
    InsufficientStock,
    InvalidQuantity,
    ProductNotFound,
    ReservationExpired,
    ReservationNotFound,
    ReservationStatus,
)
from app.store import ReservationStore

TTL = 300  # 5 minutos


def make_store(clock: FakeClock | None = None, ttl: int = TTL) -> ReservationStore:
    return ReservationStore(
        initial_stock={("store-1", "product-1"): 10, ("store-2", "product-1"): 5},
        clock=clock or FakeClock(),
        ttl_seconds=ttl,
    )


# --- Disponibilidad ---------------------------------------------------------


def test_disponibilidad_inicial():
    store = make_store()
    assert store.availability("store-1", "product-1") == 10
    assert store.availability("store-2", "product-1") == 5


def test_stock_independiente_por_tienda():
    store = make_store()
    store.reserve(
        user_id="u1", store_id="store-1", product_id="product-1",
        quantity=4, idempotency_key="k1",
    )
    # La reserva en store-1 no afecta a store-2.
    assert store.availability("store-1", "product-1") == 6
    assert store.availability("store-2", "product-1") == 5


def test_producto_inexistente():
    store = make_store()
    with pytest.raises(ProductNotFound):
        store.availability("store-1", "producto-fantasma")


# --- Creación de reservas ---------------------------------------------------


def test_reserva_descuenta_disponibilidad():
    store = make_store()
    res, created = store.reserve(
        user_id="u1", store_id="store-1", product_id="product-1",
        quantity=3, idempotency_key="k1",
    )
    assert created is True
    assert res.status is ReservationStatus.ACTIVE
    assert store.availability("store-1", "product-1") == 7


def test_reserva_sin_stock_no_modifica_inventario():
    store = make_store()
    with pytest.raises(InsufficientStock):
        store.reserve(
            user_id="u1", store_id="store-1", product_id="product-1",
            quantity=11, idempotency_key="k1",
        )
    assert store.availability("store-1", "product-1") == 10


def test_cantidad_invalida():
    store = make_store()
    with pytest.raises(InvalidQuantity):
        store.reserve(
            user_id="u1", store_id="store-1", product_id="product-1",
            quantity=0, idempotency_key="k1",
        )


# --- Idempotencia -----------------------------------------------------------


def test_idempotencia_mismos_datos_devuelve_misma_reserva():
    store = make_store()
    res1, created1 = store.reserve(
        user_id="u1", store_id="store-1", product_id="product-1",
        quantity=2, idempotency_key="checkout-abc",
    )
    res2, created2 = store.reserve(
        user_id="u1", store_id="store-1", product_id="product-1",
        quantity=2, idempotency_key="checkout-abc",
    )
    assert created1 is True
    assert created2 is False  # reintento, no creación
    assert res1.id == res2.id
    # El descuento ocurrió una sola vez.
    assert store.availability("store-1", "product-1") == 8


def test_idempotencia_datos_distintos_es_error():
    store = make_store()
    store.reserve(
        user_id="u1", store_id="store-1", product_id="product-1",
        quantity=2, idempotency_key="checkout-abc",
    )
    with pytest.raises(IdempotencyConflict):
        store.reserve(
            user_id="u1", store_id="store-1", product_id="product-1",
            quantity=3, idempotency_key="checkout-abc",
        )


def test_idempotencia_clave_sobre_reserva_vencida_no_crea_nueva():
    clock = FakeClock()
    store = make_store(clock)
    res1, _ = store.reserve(
        user_id="u1", store_id="store-1", product_id="product-1",
        quantity=2, idempotency_key="checkout-abc",
    )
    # La reserva vence...
    clock.advance(TTL)
    assert store.availability("store-1", "product-1") == 10  # stock recuperado
    # ...y reusar la clave devuelve la MISMA reserva (vencida), no crea otra.
    res2, created = store.reserve(
        user_id="u1", store_id="store-1", product_id="product-1",
        quantity=2, idempotency_key="checkout-abc",
    )
    assert created is False
    assert res2.id == res1.id
    assert res2.effective_status(clock.now()) is ReservationStatus.EXPIRED
    # No se volvió a descontar.
    assert store.availability("store-1", "product-1") == 10


def test_idempotencia_clave_sobre_reserva_confirmada_no_crea_nueva():
    store = make_store()
    res1, _ = store.reserve(
        user_id="u1", store_id="store-1", product_id="product-1",
        quantity=2, idempotency_key="checkout-abc",
    )
    store.confirm(res1.id)
    res2, created = store.reserve(
        user_id="u1", store_id="store-1", product_id="product-1",
        quantity=2, idempotency_key="checkout-abc",
    )
    assert created is False
    assert res2.id == res1.id


# --- Confirmación -----------------------------------------------------------


def test_confirmar_mantiene_unidades_descontadas():
    store = make_store()
    res, _ = store.reserve(
        user_id="u1", store_id="store-1", product_id="product-1",
        quantity=3, idempotency_key="k1",
    )
    assert store.availability("store-1", "product-1") == 7
    confirmed = store.confirm(res.id)
    assert confirmed.status is ReservationStatus.CONFIRMED
    # Confirmar no cambia la disponibilidad: held -> sold, ambas restan.
    assert store.availability("store-1", "product-1") == 7


def test_confirmacion_repetida_es_idempotente():
    store = make_store()
    res, _ = store.reserve(
        user_id="u1", store_id="store-1", product_id="product-1",
        quantity=3, idempotency_key="k1",
    )
    store.confirm(res.id)
    store.confirm(res.id)
    store.confirm(res.id)
    # No hay doble descuento por confirmar varias veces.
    assert store.availability("store-1", "product-1") == 7


def test_confirmar_inexistente_es_error():
    store = make_store()
    with pytest.raises(ReservationNotFound):
        store.confirm("no-existe")


# --- Vencimiento e interacción con confirmación -----------------------------


def test_vencimiento_recupera_stock():
    clock = FakeClock()
    store = make_store(clock)
    store.reserve(
        user_id="u1", store_id="store-1", product_id="product-1",
        quantity=4, idempotency_key="k1",
    )
    assert store.availability("store-1", "product-1") == 6
    # Justo antes del vencimiento sigue retenida.
    clock.advance(TTL - 1)
    assert store.availability("store-1", "product-1") == 6
    # Al alcanzar el vencimiento (now >= expires_at) las unidades vuelven.
    clock.advance(1)
    assert store.availability("store-1", "product-1") == 10


def test_reserva_vencida_no_puede_confirmarse():
    clock = FakeClock()
    store = make_store(clock)
    res, _ = store.reserve(
        user_id="u1", store_id="store-1", product_id="product-1",
        quantity=4, idempotency_key="k1",
    )
    clock.advance(TTL)
    with pytest.raises(ReservationExpired):
        store.confirm(res.id)


def test_confirmada_no_vence_despues():
    clock = FakeClock()
    store = make_store(clock)
    res, _ = store.reserve(
        user_id="u1", store_id="store-1", product_id="product-1",
        quantity=4, idempotency_key="k1",
    )
    # Se confirma antes del vencimiento.
    clock.advance(TTL - 1)
    store.confirm(res.id)
    # Aunque pase el tiempo original de vencimiento, sigue vendida (no se libera).
    clock.advance(TTL * 10)
    assert store.availability("store-1", "product-1") == 6
    # Y confirmarla de nuevo sigue siendo exitoso.
    again = store.confirm(res.id)
    assert again.status is ReservationStatus.CONFIRMED


def test_stock_liberado_por_vencimiento_se_puede_reservar():
    clock = FakeClock()
    store = make_store(clock)
    store.reserve(
        user_id="u1", store_id="store-1", product_id="product-1",
        quantity=10, idempotency_key="k1",
    )
    assert store.availability("store-1", "product-1") == 0
    with pytest.raises(InsufficientStock):
        store.reserve(
            user_id="u2", store_id="store-1", product_id="product-1",
            quantity=1, idempotency_key="k2",
        )
    clock.advance(TTL)
    # Tras el vencimiento, otro usuario puede reservar.
    res, created = store.reserve(
        user_id="u2", store_id="store-1", product_id="product-1",
        quantity=10, idempotency_key="k3",
    )
    assert created is True
    assert store.availability("store-1", "product-1") == 0


# --- Concurrencia -----------------------------------------------------------


def test_no_sobreventa_bajo_concurrencia():
    """50 usuarios compiten por 10 unidades (1 c/u). Exactamente 10 ganan."""
    store = make_store()
    attempts = 50

    def try_reserve(i: int) -> bool:
        try:
            store.reserve(
                user_id=f"u{i}", store_id="store-1", product_id="product-1",
                quantity=1, idempotency_key=f"k{i}",
            )
            return True
        except InsufficientStock:
            return False

    with ThreadPoolExecutor(max_workers=32) as pool:
        results = list(pool.map(try_reserve, range(attempts)))

    assert sum(results) == 10
    available = store.availability("store-1", "product-1")
    assert available == 0
    assert available >= 0  # nunca negativo


def test_concurrencia_ultima_unidad():
    """Muchos compiten por la última unidad: solo uno la obtiene."""
    store = make_store()
    # Dejar exactamente 1 disponible.
    store.reserve(
        user_id="base", store_id="store-1", product_id="product-1",
        quantity=9, idempotency_key="base",
    )
    assert store.availability("store-1", "product-1") == 1

    def try_reserve(i: int) -> bool:
        try:
            store.reserve(
                user_id=f"u{i}", store_id="store-1", product_id="product-1",
                quantity=1, idempotency_key=f"k{i}",
            )
            return True
        except InsufficientStock:
            return False

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(try_reserve, range(20)))

    assert sum(results) == 1
    assert store.availability("store-1", "product-1") == 0

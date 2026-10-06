"""Tests de integración de la capa HTTP.

Se inyecta un FakeClock para poder demostrar el vencimiento vía la API sin
esperar tiempo real.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.clock import FakeClock
from app.store import ReservationStore

TTL = 300


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def client(clock: FakeClock) -> TestClient:
    store = ReservationStore(
        initial_stock={("store-1", "product-1"): 10, ("store-2", "product-1"): 5},
        clock=clock,
        ttl_seconds=TTL,
    )
    return TestClient(create_app(store))


def _reserve(client: TestClient, **overrides):
    body = {
        "user_id": "user-123",
        "store_id": "store-1",
        "product_id": "product-1",
        "quantity": 2,
        "idempotency_key": "checkout-abc",
    }
    body.update(overrides)
    return client.post("/reservations", json=body)


def test_availability_endpoint(client: TestClient):
    r = client.get("/availability", params={"store_id": "store-1", "product_id": "product-1"})
    assert r.status_code == 200
    assert r.json() == {"store_id": "store-1", "product_id": "product-1", "available": 10}


def test_availability_producto_inexistente_404(client: TestClient):
    r = client.get("/availability", params={"store_id": "store-1", "product_id": "nope"})
    assert r.status_code == 404
    assert r.json()["code"] == "product_not_found"


def test_crear_reserva_201(client: TestClient):
    r = _reserve(client)
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "active"
    assert body["quantity"] == 2
    assert body["reservation_id"]
    assert body["expires_at"] > body["created_at"]


def test_crear_reserva_sin_stock_409(client: TestClient):
    r = _reserve(client, quantity=11, idempotency_key="k-big")
    assert r.status_code == 409
    assert r.json()["code"] == "insufficient_stock"


def test_cantidad_invalida_422(client: TestClient):
    r = _reserve(client, quantity=0, idempotency_key="k-zero")
    assert r.status_code == 422  # validación de Pydantic


def test_idempotencia_mismos_datos_200_y_misma_reserva(client: TestClient):
    r1 = _reserve(client)
    r2 = _reserve(client)
    assert r1.status_code == 201
    assert r2.status_code == 200  # reintento
    assert r1.json()["reservation_id"] == r2.json()["reservation_id"]
    avail = client.get("/availability", params={"store_id": "store-1", "product_id": "product-1"})
    assert avail.json()["available"] == 8  # descontado una sola vez


def test_idempotencia_datos_distintos_409(client: TestClient):
    _reserve(client)
    r = _reserve(client, quantity=5)
    assert r.status_code == 409
    assert r.json()["code"] == "idempotency_conflict"


def test_confirmar_reserva(client: TestClient):
    res = _reserve(client).json()
    r = client.post(f"/reservations/{res['reservation_id']}/confirm")
    assert r.status_code == 200
    assert r.json()["status"] == "confirmed"


def test_confirmacion_repetida_200(client: TestClient):
    res = _reserve(client).json()
    rid = res["reservation_id"]
    r1 = client.post(f"/reservations/{rid}/confirm")
    r2 = client.post(f"/reservations/{rid}/confirm")
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r2.json()["status"] == "confirmed"


def test_confirmar_inexistente_404(client: TestClient):
    r = client.post("/reservations/no-existe/confirm")
    assert r.status_code == 404
    assert r.json()["code"] == "reservation_not_found"


def test_vencimiento_via_api(client: TestClient, clock: FakeClock):
    res = _reserve(client, quantity=4, idempotency_key="k1").json()
    avail = client.get("/availability", params={"store_id": "store-1", "product_id": "product-1"})
    assert avail.json()["available"] == 6

    clock.advance(TTL)  # vence
    avail = client.get("/availability", params={"store_id": "store-1", "product_id": "product-1"})
    assert avail.json()["available"] == 10

    # Confirmar una vencida falla.
    r = client.post(f"/reservations/{res['reservation_id']}/confirm")
    assert r.status_code == 409
    assert r.json()["code"] == "reservation_expired"

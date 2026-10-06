#!/usr/bin/env python3
"""Demo end-to-end del servicio de reservas, auto-verificable.

Recorre, contra el servicio HTTP real, todas las garantías que el ejercicio pide
revisar en la demostración:

  A. Consultar disponibilidad
  B. Flujo feliz: crear + confirmar
  C. Reintentos con la misma clave de idempotencia
  D. Confirmaciones repetidas
  E. Competencia de múltiples solicitudes por stock limitado (concurrencia)
  F. Vencimiento y recuperación del stock
  G. Interacción entre confirmación y vencimiento

Cada escenario arranca un servidor fresco (estado en memoria limpio), de modo que
los números siempre coinciden con el seed inicial y se pueden narrar sin cuentas.

Uso:
    source .venv/bin/activate
    python demo.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import httpx

PORT = 8077
BASE = f"http://127.0.0.1:{PORT}"
TTL = 3  # segundos: corto para poder demostrar el vencimiento sin esperar

# --- salida con color -------------------------------------------------------

G, R, Y, B, DIM, RST = "\033[32m", "\033[31m", "\033[33m", "\033[36m", "\033[2m", "\033[0m"
_failures = 0


def title(text: str) -> None:
    print(f"\n{B}{'=' * 70}\n{text}\n{'=' * 70}{RST}")


def step(text: str) -> None:
    print(f"\n{Y}▶ {text}{RST}")


def show(method: str, path: str, resp: httpx.Response) -> None:
    color = G if resp.status_code < 400 else R
    print(f"  {DIM}{method} {path}{RST} → {color}{resp.status_code}{RST}  {resp.text}")


def check(label: str, ok: bool) -> None:
    global _failures
    mark = f"{G}✓{RST}" if ok else f"{R}✗{RST}"
    if not ok:
        _failures += 1
    print(f"  {mark} {label}")


# --- manejo del servidor ----------------------------------------------------


@contextmanager
def server(ttl: int = TTL):
    """Levanta un servidor fresco con el TTL indicado y lo frena al salir."""
    env = dict(os.environ, RESERVATION_TTL_SECONDS=str(ttl))
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app", "--port", str(PORT), "--log-level", "warning"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(100):
            try:
                if httpx.get(f"{BASE}/health", timeout=0.3).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.1)
        else:
            raise RuntimeError("el servidor no respondió a tiempo")
        yield
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


# --- helpers de API ---------------------------------------------------------


def availability(c: httpx.Client, store="store-1", product="product-1") -> int:
    r = c.get(f"{BASE}/availability", params={"store_id": store, "product_id": product})
    show("GET ", "/availability", r)
    return r.json()["available"]


def reserve(c: httpx.Client, *, user="user-123", store="store-1", product="product-1",
            qty=2, key="checkout-abc") -> httpx.Response:
    r = c.post(f"{BASE}/reservations", json={
        "user_id": user, "store_id": store, "product_id": product,
        "quantity": qty, "idempotency_key": key,
    })
    show("POST", "/reservations", r)
    return r


def confirm(c: httpx.Client, reservation_id: str) -> httpx.Response:
    r = c.post(f"{BASE}/reservations/{reservation_id}/confirm")
    show("POST", f"/reservations/{reservation_id[:8]}…/confirm", r)
    return r


# --- escenarios -------------------------------------------------------------


def scenario_a():
    title("A. Consultar disponibilidad (stock independiente por tienda)")
    with server(), httpx.Client() as c:
        step("Disponibilidad inicial de cada tienda")
        s1 = availability(c, "store-1")
        s2 = availability(c, "store-2")
        check("store-1 / product-1 = 10", s1 == 10)
        check("store-2 / product-1 = 5", s2 == 5)


def scenario_b():
    title("B. Flujo feliz: crear una reserva y confirmarla")
    with server(), httpx.Client() as c:
        step("Crear reserva de 2 unidades")
        r = reserve(c, qty=2, key="checkout-feliz")
        check("respondió 201 (creada)", r.status_code == 201)
        rid = r.json()["reservation_id"]

        step("La disponibilidad bajó de 10 a 8")
        check("disponible = 8", availability(c) == 8)

        step("Confirmar la compra")
        rc = confirm(c, rid)
        check("respondió 200 y status 'confirmed'",
              rc.status_code == 200 and rc.json()["status"] == "confirmed")

        step("Confirmar no vuelve a descontar (sigue en 8)")
        check("disponible = 8", availability(c) == 8)


def scenario_c():
    title("C. Reintentos con la misma clave de idempotencia")
    with server(), httpx.Client() as c:
        step("Primera creación con key 'checkout-abc'")
        r1 = reserve(c, qty=2, key="checkout-abc")
        rid1 = r1.json()["reservation_id"]
        check("201 (creada)", r1.status_code == 201)
        check("disponible = 8", availability(c) == 8)

        step("Reintento con la MISMA clave y los MISMOS datos")
        r2 = reserve(c, qty=2, key="checkout-abc")
        check("200 (reintento, no creación)", r2.status_code == 200)
        check("devuelve la MISMA reserva", r2.json()["reservation_id"] == rid1)
        check("NO volvió a descontar (sigue en 8)", availability(c) == 8)

        step("Misma clave con datos DISTINTOS (quantity=5)")
        r3 = reserve(c, qty=5, key="checkout-abc")
        check("409 idempotency_conflict",
              r3.status_code == 409 and r3.json()["code"] == "idempotency_conflict")


def scenario_d():
    title("D. Confirmaciones repetidas (idempotente)")
    with server(), httpx.Client() as c:
        r = reserve(c, qty=3, key="k-confirm")
        rid = r.json()["reservation_id"]
        check("disponible = 7", availability(c) == 7)

        step("Confirmar tres veces la misma reserva")
        codes = [confirm(c, rid).status_code for _ in range(3)]
        check("las tres respondieron 200", codes == [200, 200, 200])
        check("sin doble descuento (sigue en 7)", availability(c) == 7)


def scenario_e():
    title("E. Competencia por stock limitado (concurrencia)")
    with server(), httpx.Client() as c:
        attempts = 50
        step(f"{attempts} usuarios intentan reservar 1 unidad a la vez (hay 10)")

        def try_reserve(i: int) -> int:
            return c.post(f"{BASE}/reservations", json={
                "user_id": f"u{i}", "store_id": "store-1", "product_id": "product-1",
                "quantity": 1, "idempotency_key": f"k{i}",
            }).status_code

        with ThreadPoolExecutor(max_workers=32) as pool:
            codes = list(pool.map(try_reserve, range(attempts)))

        ok = sum(1 for c_ in codes if c_ == 201)
        rejected = sum(1 for c_ in codes if c_ == 409)
        print(f"  {DIM}aceptadas (201): {ok}   rechazadas (409): {rejected}{RST}")
        check("exactamente 10 reservas aceptadas (no hay sobreventa)", ok == 10)
        check("el resto rechazado por falta de stock", rejected == attempts - 10)
        final = availability(c)
        check("disponible = 0 (nunca negativo)", final == 0)


def scenario_f():
    title("F. Vencimiento y recuperación del stock")
    with server(ttl=TTL), httpx.Client() as c:
        step(f"Crear reserva de 4 (TTL = {TTL}s para la demo)")
        r = reserve(c, qty=4, key="k-vence")
        rid = r.json()["reservation_id"]
        check("disponible = 6", availability(c) == 6)

        step(f"Esperar a que venza ({TTL + 1}s)…")
        time.sleep(TTL + 1)

        step("Las unidades vuelven a estar disponibles")
        check("disponible = 10 (stock recuperado)", availability(c) == 10)

        step("Una reserva vencida NO se puede confirmar")
        rc = confirm(c, rid)
        check("409 reservation_expired",
              rc.status_code == 409 and rc.json()["code"] == "reservation_expired")


def scenario_g():
    title("G. Interacción entre confirmación y vencimiento")
    with server(ttl=TTL), httpx.Client() as c:
        step("Crear reserva de 4 y confirmarla ANTES de que venza")
        r = reserve(c, qty=4, key="k-confirm-vence")
        rid = r.json()["reservation_id"]
        rc = confirm(c, rid)
        check("confirmada (200)", rc.status_code == 200)

        step(f"Dejar pasar el tiempo de vencimiento ({TTL + 1}s)…")
        time.sleep(TTL + 1)

        step("Una reserva CONFIRMADA no se libera (las unidades siguen vendidas)")
        check("disponible = 6 (no volvió a 10)", availability(c) == 6)

        step("Y confirmarla de nuevo sigue siendo exitoso")
        rc2 = confirm(c, rid)
        check("200 y status 'confirmed'",
              rc2.status_code == 200 and rc2.json()["status"] == "confirmed")


def main() -> int:
    print(f"{B}Demo end-to-end — Reservas de stock para quick commerce{RST}")
    print(f"{DIM}Servidor en {BASE} · TTL de demo = {TTL}s{RST}")
    for scenario in (scenario_a, scenario_b, scenario_c, scenario_d,
                     scenario_e, scenario_f, scenario_g):
        scenario()

    title("Resumen")
    if _failures == 0:
        print(f"{G}✓ Todas las garantías verificadas correctamente.{RST}\n")
        return 0
    print(f"{R}✗ {_failures} verificación(es) fallaron.{RST}\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

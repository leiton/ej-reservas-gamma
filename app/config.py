"""Configuración y datos iniciales."""

from __future__ import annotations

import os

from .store import StockKey

# TTL de una reserva (vencimiento a los 5 minutos por defecto). Configurable por
# variable de entorno para poder demostrar el vencimiento con un valor chico.
DEFAULT_TTL_SECONDS = int(os.environ.get("RESERVATION_TTL_SECONDS", "300"))


def initial_stock() -> dict[StockKey, int]:
    """Stock físico sembrado al arrancar (reponer inventario está fuera de alcance)."""
    return {
        ("store-1", "product-1"): 10,
        ("store-2", "product-1"): 5,
    }

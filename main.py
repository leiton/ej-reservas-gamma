"""Punto de entrada: arma el store con datos reales y expone la app.

    uvicorn main:app --reload
"""

from __future__ import annotations

from app.api import create_app
from app.clock import SystemClock
from app.config import DEFAULT_TTL_SECONDS, initial_stock
from app.store import ReservationStore

store = ReservationStore(
    initial_stock=initial_stock(),
    clock=SystemClock(),
    ttl_seconds=DEFAULT_TTL_SECONDS,
)
app = create_app(store)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)

"""Reloj inyectable.

Permite usar el tiempo real en producción y un reloj controlable en los tests,
de modo que el vencimiento de reservas se pueda demostrar sin esperar 5 minutos.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol


class Clock(Protocol):
    """Fuente de tiempo. Siempre devuelve datetimes *aware* en UTC."""

    def now(self) -> datetime:  # pragma: no cover - interfaz
        ...


class SystemClock:
    """Reloj real, para producción."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FakeClock:
    """Reloj controlable, para tests y demos.

    Arranca en un instante fijo y solo avanza cuando se lo pide explícitamente.
    """

    def __init__(self, start: datetime | None = None) -> None:
        if start is None:
            start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        if start.tzinfo is None:
            raise ValueError("FakeClock requiere un datetime aware (con tzinfo)")
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        """Avanza el reloj la cantidad de segundos indicada."""
        self._now = self._now + timedelta(seconds=seconds)

    def set(self, moment: datetime) -> None:
        if moment.tzinfo is None:
            raise ValueError("Se requiere un datetime aware (con tzinfo)")
        self._now = moment

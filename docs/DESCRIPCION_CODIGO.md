# Descripción del código — Servicio de reservas de stock

Servicio HTTP que gestiona reservas temporales de stock para quick commerce:
reservar unidades durante el checkout, confirmarlas (venta) o dejar que venzan y
vuelvan a estar disponibles.

**Stack:** Python 3.10 · FastAPI · almacenamiento en memoria.

---

## 1. Arquitectura y estructura

El código está organizado en **capas** con una dependencia unidireccional: la
capa HTTP depende del dominio, nunca al revés. El dominio no sabe que existe
FastAPI, lo que lo hace testeable de forma aislada y rápida.

```
main.py              Punto de entrada: arma el store real y expone `app`
app/
  domain.py          Entidad Reservation, estados y errores de negocio
  clock.py           Reloj inyectable (SystemClock / FakeClock)
  store.py           Núcleo: lógica de reservas + almacenamiento + locking
  schemas.py         Modelos de entrada/salida (Pydantic)
  api.py             Capa HTTP: endpoints y traducción de errores a HTTP
  config.py          TTL configurable y datos iniciales (seed)
tests/
  test_store.py      Tests del dominio (unidad, concurrencia, vencimiento)
  test_api.py        Tests de integración HTTP
```

El flujo de una request es: `api.py` (parseo y validación) → `store.py` (lógica y
estado) → `domain.py` (entidad y reglas). Los errores de negocio nacen como
excepciones en el dominio y `api.py` las traduce a códigos HTTP.

---

## 2. Modelo de dominio (`domain.py`)

La entidad central es `Reservation`, un `dataclass` con los datos de la reserva
más dos marcas de tiempo (`created_at`, `expires_at`) y un `status`.

**Decisión clave sobre los estados:** solo se persisten dos, `ACTIVE` y
`CONFIRMED`. El estado `EXPIRED` **no se guarda**: se *deriva* del tiempo. El
método `effective_status(now)` calcula el estado real en cada consulta:

```python
def effective_status(self, now):
    if self.status is CONFIRMED:   return CONFIRMED      # terminal, nunca vence
    if now >= self.expires_at:     return EXPIRED        # derivado del tiempo
    return ACTIVE
```

Esto implementa el **vencimiento perezoso** que pide el enunciado: no hace falta
un proceso en segundo plano que recorra reservas y las marque vencidas; una
reserva activa cuyo `expires_at` ya pasó simplemente *se comporta* como vencida
la próxima vez que se la consulta.

Los **errores de negocio** son una jerarquía de excepciones con un atributo
`code` (`InsufficientStock`, `IdempotencyConflict`, `ReservationNotFound`,
`ReservationExpired`, `ProductNotFound`, `InvalidQuantity`). El dominio las lanza;
la capa HTTP las mapea a un status code.

---

## 3. El núcleo: `ReservationStore` (`store.py`)

Es el corazón de la solución: contiene el estado compartido y las tres
operaciones del negocio.

### 3.1. Almacenamiento en memoria: tres diccionarios

```python
self._totals: dict[(store_id, product_id), int]   # stock físico total (seed, fijo)
self._reservations: dict[reservation_id, Reservation]
self._idempotency: dict[idempotency_key, reservation_id]
```

- **`_totals`** guarda el stock físico **total** por `(tienda, producto)`. Es
  fijo (se siembra al arrancar; reponer inventario está fuera de alcance).
- **`_reservations`** guarda cada reserva por su id, con su estado.
- **`_idempotency`** es un índice que mapea cada clave de idempotencia a la
  reserva que creó, para detectar reintentos.

### 3.2. La disponibilidad es derivada, no almacenada

No existe un contador mutable de "disponible". Se **calcula** en cada consulta
recorriendo las reservas de ese producto (`_available_locked`):

```
disponible = total − Σ(cantidad de ACTIVE no vencidas) − Σ(cantidad de CONFIRMED)
```

Las reservas `EXPIRED` no se restan, así que sus unidades "vuelven" solas. Esta
decisión tiene dos consecuencias que simplifican la correctitud:

- **Vencimiento automático**, sin bookkeeping ni jobs.
- **Confirmar no toca inventario**: solo reclasifica una reserva de `ACTIVE`
  (retenida) a `CONFIRMED` (vendida), y como ambas ya restaban, **no hay riesgo
  de doble descuento**.

El trade-off es que la lectura es O(n) sobre las reservas del producto;
aceptable a esta escala, y en el README se indica cómo evolucionarlo.

### 3.3. Concurrencia: un lock global

Todas las operaciones que leen-modifican-escriben el estado corren dentro de un
único `threading.Lock`:

```python
with self._lock:
    # leer disponibilidad → decidir → crear/confirmar  (atómico)
```

Esto cierra la ventana del *check-then-act*: sin el lock, dos requests podrían
leer "queda 1 unidad" antes de que cualquiera descuente, y ambas reservar
(sobreventa). El lock serializa la sección crítica, garantizando que las
solicitudes concurrentes **nunca reserven más unidades de las existentes** y que
el disponible **nunca sea negativo**. Es la opción más obviamente correcta; el
README menciona el lock por `(tienda, producto)` como evolución para más
throughput.

### 3.4. Las tres operaciones

**`availability(store_id, product_id)`** — toma el lock y devuelve el disponible
derivado. Si el par no existe, lanza `ProductNotFound`.

**`reserve(...)`** — devuelve `(reserva, created)`:

1. Si la `idempotency_key` **ya existe**: compara los datos del request con los
   de la reserva original. Mismos datos → devuelve la misma reserva sin
   descontar (`created=False`). Datos distintos → `IdempotencyConflict`.
2. Si la clave es **nueva**: calcula el disponible. Si no alcanza →
   `InsufficientStock` (sin tocar inventario ni guardar la clave). Si alcanza →
   crea la reserva (`status=ACTIVE`, `expires_at = now + TTL`), la guarda y
   registra la clave (`created=True`).

**`confirm(reservation_id)`** — busca la reserva y según su estado efectivo:

- `CONFIRMED` → la devuelve tal cual (confirmación repetida idempotente, sin
  volver a descontar).
- `EXPIRED` → `ReservationExpired`.
- `ACTIVE` → la pasa a `CONFIRMED`.
- No existe → `ReservationNotFound`.

---

## 4. Reloj inyectable (`clock.py`)

El tiempo se abstrae tras una interfaz `Clock` con un método `now()`:

- **`SystemClock`** devuelve la hora real (UTC). Se usa en producción.
- **`FakeClock`** arranca en un instante fijo y solo avanza cuando se lo pide
  (`advance(segundos)`). Se usa en los tests.

El store recibe el reloj por constructor, nunca llama a `datetime.now()`
directamente. Gracias a esto, el vencimiento se puede demostrar y testear de
forma **determinística, sin esperar 5 minutos reales**: se crea una reserva, se
avanza el `FakeClock` más allá del TTL y se verifica que el stock se recuperó.
El TTL además es configurable (variable de entorno `RESERVATION_TTL_SECONDS`,
default 300).

---

## 5. Capa HTTP (`api.py`, `schemas.py`)

Expone tres endpoints mediante una *factory* `create_app(store)` que recibe el
store ya construido (facilita inyectar uno de prueba en los tests).

| Método | Ruta | Operación |
|--------|------|-----------|
| GET  | `/availability?store_id=&product_id=` | Consultar disponibilidad |
| POST | `/reservations` | Crear una reserva |
| POST | `/reservations/{id}/confirm` | Confirmar una reserva |

**Validación de entrada:** los modelos Pydantic (`schemas.py`) validan el cuerpo
antes de llegar al handler. Por ejemplo `quantity: int = Field(gt=0)` rechaza
cantidades no positivas con un `422` automático.

**Handlers síncronos (`def`):** FastAPI los ejecuta en un threadpool, es decir
con hilos reales del sistema. Por eso la garantía de no-sobreventa del lock se
ejercita de verdad bajo concurrencia (no es una seguridad "gratis" de un event
loop single-thread).

**Dos piezas de FastAPI que vale la pena notar:**

- `store: ReservationStore = Depends(get_store)` — inyección de dependencias:
  FastAPI llama a `get_store()` antes del handler y le pasa el store. Desacopla
  el handler del origen del store y permite sobrescribirlo en tests.
- `response: Response` + `response.status_code = 201 if created else 200` — deja
  decidir el código **en runtime**: `201` si la reserva se creó, `200` si fue un
  reintento idempotente.

**Traducción de errores:** un `exception_handler(DomainError)` centraliza el
mapeo de cada excepción de dominio a su status HTTP y responde en JSON con la
forma `{"error": "...", "code": "..."}`:

| Excepción | HTTP |
|-----------|------|
| `ProductNotFound`, `ReservationNotFound` | 404 |
| `InsufficientStock`, `IdempotencyConflict`, `ReservationExpired` | 409 |
| `InvalidQuantity` | 422 |

Así los handlers quedan limpios (solo la lógica feliz) y el manejo de errores
vive en un solo lugar.

---

## 6. Resumen de decisiones de diseño

- **Modelo de disponibilidad derivado** (`total − activas − confirmadas`) en vez
  de un contador mutable: hace el vencimiento automático y elimina el riesgo de
  doble descuento al confirmar.
- **Vencimiento perezoso**: `EXPIRED` es un estado calculado, sin job en
  background.
- **Lock global + handlers síncronos**: la forma más evidente de garantizar
  no-sobreventa, ejercitada con hilos reales.
- **Idempotencia** con índice `clave → reserva` y comparación de datos, que
  distingue reintento (mismos datos → misma reserva) de conflicto (datos
  distintos → 409).
- **Reloj inyectable** para un vencimiento testeable y determinístico.
- **Separación en capas** (dominio / HTTP) para aislar la lógica de negocio del
  framework.

**Limitaciones** (detalladas en el README): el estado en memoria se pierde al
reiniciar y no se comparte entre instancias; el lock global serializa todo
(límite de throughput); la lectura de disponibilidad es O(n). Los **próximos
pasos** apuntan a persistencia (Postgres/Redis con no-oversell delegado a la
base), locking más fino y deduplicación semántica más allá de la clave de
idempotencia.

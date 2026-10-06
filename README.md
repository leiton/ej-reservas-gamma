# Reservas de stock — quick commerce

Servicio HTTP que gestiona reservas temporales de stock durante el checkout:
reservar unidades, confirmarlas (venta) o dejar que venzan y vuelvan a estar
disponibles.

Stack: **Python 3.10+ / FastAPI**. Almacenamiento **en memoria**.

## Cómo ejecutarlo

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

uvicorn main:app --port 8000
```

Docs interactivas (Swagger) en <http://127.0.0.1:8000/docs>.

El vencimiento de reservas es de **5 minutos** por defecto. Para demos es cómodo
bajarlo con una variable de entorno:

```bash
RESERVATION_TTL_SECONDS=5 uvicorn main:app --port 8000
```

## Cómo correr las pruebas

```bash
source .venv/bin/activate
pytest
```

Son 30 tests (núcleo de dominio + integración HTTP). Los de vencimiento usan un
**reloj inyectable** (`FakeClock`), así que corren instantáneamente sin esperar
tiempo real.

## Datos iniciales

| Tienda  | Producto  | Stock |
|---------|-----------|-------|
| store-1 | product-1 | 10    |
| store-2 | product-1 | 5     |

El stock de un producto es independiente entre tiendas. No hay API para cargar o
reponer inventario (fuera de alcance): el total se siembra al arrancar.

## API

### 1. Consultar disponibilidad

```bash
curl "http://127.0.0.1:8000/availability?store_id=store-1&product_id=product-1"
# {"store_id":"store-1","product_id":"product-1","available":10}
```

### 2. Crear una reserva

```bash
curl -X POST http://127.0.0.1:8000/reservations \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "user-123",
    "store_id": "store-1",
    "product_id": "product-1",
    "quantity": 2,
    "idempotency_key": "checkout-abc"
  }'
```

- `201` con la reserva (incluye `reservation_id`, `status`, `expires_at`).
- `200` si es un reintento con la **misma clave y mismos datos** (devuelve la
  misma reserva, sin volver a descontar).
- `409 insufficient_stock` si no hay stock (no modifica inventario).
- `409 idempotency_conflict` si se reusa la clave con **datos distintos**.
- `422` si la cantidad no es un entero positivo.

### 3. Confirmar una reserva

```bash
curl -X POST http://127.0.0.1:8000/reservations/<reservation_id>/confirm
```

- `200 confirmed` al confirmar una reserva activa.
- `200 confirmed` también ante confirmaciones repetidas (idempotente, sin doble
  descuento).
- `409 reservation_expired` si la reserva ya venció.
- `404 reservation_not_found` si el id no existe.

Formato de error: `{"error": "...", "code": "..."}`.

## Decisiones de diseño

**Modelo de disponibilidad derivado.** No hay un contador mutable de
"disponible". El total físico por `(tienda, producto)` es fijo y la
disponibilidad se calcula en cada operación:

```
disponible = total − unidades_activas_no_vencidas − unidades_confirmadas
```

Consecuencias que simplifican la correctitud:

- **Vencimiento perezoso, sin job en background.** Una reserva está vencida si
  `now >= expires_at`; es un estado *derivado* del tiempo, no requiere mutar
  nada ni un proceso que barra vencimientos. Las unidades vuelven a estar
  disponibles automáticamente en la siguiente consulta.
- **Confirmar no toca inventario.** Solo reclasifica la reserva de "activa"
  (held) a "confirmada" (sold); como ambas restan de la disponibilidad, no hay
  riesgo de descontar dos veces.

**Concurrencia con un lock global.** Todas las operaciones que
leen-modifican-escriben el estado se serializan con un único `threading.Lock`.
Es la forma más obviamente correcta de garantizar que solicitudes concurrentes
nunca reserven más unidades de las existentes, y que el stock nunca sea
negativo. Los handlers son síncronos (`def`), por lo que FastAPI los corre en su
threadpool: hay hilos reales del SO y la garantía se ejercita de verdad (ver
`test_no_sobreventa_bajo_concurrencia`).

**Idempotencia.** La `idempotency_key` es única en todo el servicio, con un
índice `clave → reserva` y comparación de los datos del request:

- misma clave + mismos datos → devuelve la misma reserva (sin re-descontar);
- misma clave + datos distintos → `409`;
- una clave ya asociada a una reserva (incluso vencida o confirmada) nunca crea
  una nueva;
- los rechazos por falta de stock no consumen la clave.

**Reloj inyectable.** `Clock` abstrae el tiempo: `SystemClock` en producción,
`FakeClock` avanzable en los tests. Permite demostrar vencimiento e interacción
confirmación/vencimiento de forma determinística.

## Limitaciones

- **En memoria:** el estado se pierde al reiniciar y no se comparte entre
  instancias. No sirve tal cual para correr con múltiples réplicas.
- **Lock global:** correcto pero serializa *todas* las operaciones; limita el
  throughput. Para este ejercicio prioricé correctitud evidente sobre
  rendimiento.
- **Lectura de disponibilidad O(n)** sobre las reservas de ese producto. Con
  pocos datos es irrelevante; a escala habría que mantener agregados.
- **Sin fairness:** ante concurrencia se garantiza que no haya sobreventa, pero
  no un orden determinístico de quién gana la última unidad (lo decide el
  scheduler).

## Próximos pasos

- **Persistencia** (PostgreSQL / Redis) con la disponibilidad resuelta por el
  store de datos: una reserva como `INSERT` condicionado (`... WHERE disponible
  >= cantidad`) o un `UPDATE` atómico del contador, delegando el no-oversell a
  la base. Esto habilita múltiples instancias.
- **Locking más fino** por `(tienda, producto)` (o particionado/sharding) para
  mayor throughput manteniendo el aislamiento.
- **Vencimiento activo** opcional (TTL de Redis o job) para liberar recursos,
  además del cálculo perezoso.
- **Deduplicación semántica** más allá de la idempotency_key: derivar la clave
  del `checkout_id` o una invariante "una reserva activa por
  (usuario, tienda, producto)", para evitar duplicados con claves distintas.
- **Observabilidad:** métricas de reservas creadas/confirmadas/vencidas y de
  contención del lock.
- **API de inventario** para cargar y reponer el stock total.

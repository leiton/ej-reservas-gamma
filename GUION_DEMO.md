# Guión de demo — Reservas de stock para quick commerce

Guía para los ~100 minutos de desarrollo/demostración. Cada bloque dice **qué
mostrar**, **qué comando correr** y **qué contar** (los puntos de diseño que el
evaluador quiere escuchar).

---

## 0. Preparación (1 min)

```bash
cd ejercicio_rappi
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

**Decir:** "Es un servicio HTTP en Python/FastAPI, almacenamiento en memoria.
Elegí FastAPI por rapidez de desarrollo y porque con handlers síncronos corre
sobre un threadpool real, lo que me deja demostrar la concurrencia de verdad."

---

## 1. Las pruebas automatizadas primero (3 min)

> El enunciado pide "pruebas automatizadas de las garantías más importantes".
> Arrancar por acá transmite que la correctitud está cubierta.

```bash
pytest
```

**Decir:** "30 tests: el núcleo de dominio y la capa HTTP. Las garantías clave —
no-sobreventa concurrente, idempotencia, confirmación repetida, vencimiento e
interacción confirmar/vencer. Los de tiempo usan un **reloj inyectable**, así
que son deterministas y no esperan tiempo real."

Mostrar brevemente [tests/test_store.py](tests/test_store.py):
`test_no_sobreventa_bajo_concurrencia` y `test_confirmada_no_vence_despues`.

---

## 2. La demo end-to-end automatizada (5 min)

> Recorre, contra el servicio HTTP real, los 5 focos que el enunciado lista para
> la demostración. Es auto-verificable (imprime ✓/✗).

```bash
python demo.py
```

Va narrando cada escenario (A–G). **Dejarlo correr** y comentar sobre la marcha.
Mapa de escenario → foco del enunciado:

| Escenario | Foco del enunciado |
|-----------|--------------------|
| A. Disponibilidad | Consultar disponibilidad; stock independiente por tienda |
| B. Crear + confirmar | Flujo feliz |
| C. Idempotencia | Reintentos con la misma clave |
| D. Confirmaciones repetidas | Confirmaciones repetidas |
| E. Concurrencia | Competencia por stock limitado |
| F. Vencimiento | Vencimiento y recuperación del stock |
| G. Confirmar vs vencer | Interacción confirmación/vencimiento |

---

## 3. Recorrido manual con curl (opcional, 10 min)

> Si quieren verlo "a mano". Levantar el server con TTL largo para el flujo
> normal.

```bash
uvicorn main:app --port 8000
```

En otra terminal:

```bash
# 1. Disponibilidad inicial → 10
curl -s "http://127.0.0.1:8000/availability?store_id=store-1&product_id=product-1"; echo

# 2. Crear una reserva → 201, guardamos el id
RID=$(curl -s -X POST http://127.0.0.1:8000/reservations \
  -H "Content-Type: application/json" \
  -d '{"user_id":"user-123","store_id":"store-1","product_id":"product-1","quantity":2,"idempotency_key":"checkout-abc"}' \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['reservation_id'])")
echo "reserva: $RID"

# 3. Disponibilidad → 8
curl -s "http://127.0.0.1:8000/availability?store_id=store-1&product_id=product-1"; echo

# 4. Reintento idempotente mismos datos → 200, mismo id, sigue 8
curl -s -X POST http://127.0.0.1:8000/reservations \
  -H "Content-Type: application/json" \
  -d '{"user_id":"user-123","store_id":"store-1","product_id":"product-1","quantity":2,"idempotency_key":"checkout-abc"}'; echo

# 5. Misma clave, datos distintos → 409 idempotency_conflict
curl -s -X POST http://127.0.0.1:8000/reservations \
  -H "Content-Type: application/json" \
  -d '{"user_id":"user-123","store_id":"store-1","product_id":"product-1","quantity":5,"idempotency_key":"checkout-abc"}'; echo

# 6. Confirmar → 200 confirmed
curl -s -X POST http://127.0.0.1:8000/reservations/$RID/confirm; echo

# 7. Confirmar de nuevo → 200 (idempotente)
curl -s -X POST http://127.0.0.1:8000/reservations/$RID/confirm; echo
```

> Para mostrar el **vencimiento a mano**, reiniciar el server con TTL corto
> (`RESERVATION_TTL_SECONDS=5 uvicorn main:app --port 8000`), crear una reserva,
> esperar 5s, consultar disponibilidad (se recuperó) e intentar confirmarla
> (→ 409 `reservation_expired`).

---

## 4. Decisiones técnicas — los últimos 20 min de charla

Puntos para defender (están en el [README.md](README.md) y en el código):

- **Modelo de disponibilidad derivado** en vez de contador mutable:
  `disponible = total − activas_no_vencidas − confirmadas`.
  - Vencimiento **perezoso**: estado derivado del tiempo, sin job en background.
  - Confirmar solo reclasifica activa→confirmada ⇒ **sin doble descuento**.
  - Trade-off: lectura O(n) sobre las reservas del producto (aceptable a esta
    escala; a escala real, agregados o delegar a la base).
- **Concurrencia con lock global** + handlers síncronos: la forma más
  obviamente correcta de evitar sobreventa; se ejercita con hilos reales.
  Evolución: lock por `(tienda, producto)` o sharding.
- **Idempotencia**: clave única global, índice `clave→reserva` + comparación de
  datos. Distinción entre reintento (mismos datos) y conflicto (datos
  distintos).
- **Reloj inyectable**: `SystemClock` / `FakeClock`, TTL configurable.

### Preguntas que probablemente hagan (y respuestas cortas)

- **"¿Cómo evitás duplicados si el cliente manda dos claves distintas?"**
  La idempotency_key solo deduplica *reintentos*, no duplicados semánticos. Se
  resuelve derivando la clave del `checkout_id`, o con una invariante de dominio
  "una reserva activa por (usuario, tienda, producto)".
- **"Dos requests por la última unidad, ¿a quién se la das?"**
  Garantizamos *safety* (no sobreventa), no *fairness*. Gana quien toma el lock
  primero (lo decide el scheduler). Para FCFS real: cola FIFO con número de
  secuencia por `(tienda, producto)`.
- **"¿Y si reinicio el servicio?"**
  Se pierde el estado (en memoria). Próximo paso: persistir en Postgres/Redis y
  delegar el no-oversell a la base (UPDATE condicional atómico), lo que además
  habilita correr varias instancias.

---

## Checklist de entregables (enunciado)

- [x] Servicio ejecutable localmente — `uvicorn main:app`
- [x] Instrucciones para iniciarlo y correr pruebas — [README.md](README.md)
- [x] Ejemplos de llamadas a la API — README + este guión + `demo.py`
- [x] Pruebas automatizadas de las garantías clave — `tests/` (30 tests)
- [x] Explicación de decisiones, limitaciones y próximos pasos — README

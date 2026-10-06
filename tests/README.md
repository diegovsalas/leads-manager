# Pruebas

```bash
python3 -m pytest          # suite completa
python3 -m pytest -q tests/test_cierre.py   # solo un flujo
```

## Qué cubren, y por qué estas y no otras

Cubren los **tres flujos que mueven dinero**. Son los únicos donde un error
no se ve: nadie reporta un bug de "mi comisión salió 3% más baja", se
descubre en la nómina.

| Archivo | Flujo | Lo que protege |
|---|---|---|
| `test_cierre.py` | cerrar un lead como ganado | que la venta se registre con el monto correcto, que no se duplique, que Pestex no cierre sin respaldo, y que una venta sin monto quede *pendiente* y no como comisión de cero |
| `test_asignacion.py` | reparto de leads | que el lead llegue a un vendedor de su marca, que no se pierdan campos por el camino, que el reparto mire la carga |
| `test_upsell.py` | expansión en oportunidades | clasificación desde leads/CS, permisos, montos, cancelación y reapertura sin duplicados, evidencia, forecast y sintaxis del Pipe |
| `test_metas.py` | avance contra meta | que una venta cuente en el mes en que **se cerró**, no en el que entró el lead |

Las regresiones de upsell también verifican endpoints HTTP, porque los permisos y las validaciones deben proteger todas las vías de cierre. El render del Pipe comprueba la sintaxis del JavaScript.

## Cómo corren

Contra una base Postgres dedicada (`avantex_crm_test`) que se recrea en cada
ejecución. No se usa SQLite: los modelos usan `UUID`, `ARRAY` y `JSONB` en 118
lugares, y probar contra otro motor daría confianza falsa justo donde importa.

Cada prueba arranca con las tablas vacías. No hay rollback por prueba porque el
código bajo prueba hace `commit` por su cuenta.

## Varias fijan bugs reales

Las que llevan una fecha en el docstring no son hipotéticas — son fallos que
ya ocurrieron:

- **`test_conserva_el_email`** — `asignar_lead_comercial` armaba el `Lead` con
  una lista fija de campos que no incluía `email`. Cualquier origen que lo
  trajera creaba el lead sin correo, y rompía la deduplicación, que busca por
  email antes de dar de alta.
- **`test_cuenta_por_fecha_de_cierre_no_de_creacion`** — el avance se medía por
  cuándo entró el lead. Un trato de julio cerrado en septiembre dejaba el mes
  del cierre casi vacío y no cuadraba con Comisiones.
- **`test_sin_monto_la_venta_queda_pendiente_no_en_cero`** — una venta sin monto
  no es una comisión de cero: está esperando la factura. Confundirlas es pagar
  de menos.

## Si agregas una

Que pruebe **una decisión**, no una línea. El nombre debe decir qué se rompería
si falla: `test_no_se_puede_cerrar_dos_veces` sirve, `test_cerrar_lead_2` no.

Usa `tests/fabricas.py` para construir los datos. Si una prueba necesita saber
que `especialidad_marca` es una lista, ese detalle tapa lo que se está probando.

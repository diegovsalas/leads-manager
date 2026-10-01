# Migraciones

Hoy el esquema se cambia en **dos** lugares. Es un estado de transición, a
propósito, y conviene entender por qué antes de tocar nada.

| | Qué hace | Estado |
|---|---|---|
| `_run_pending_migrations` (`avantex_crm.py`) | 690 líneas de DDL a mano, 27 bloques `try`, 49 sentencias. Corre entero en **cada arranque** | sigue siendo el que manda |
| `migraciones/` (Alembic) | migraciones versionadas | montado y probado, **todavía no corre en el arranque** |

Mientras los dos existan, **el esquema lo sigue cambiando el primero**. Alembic
está listo pero inerte: ningún código lo invoca al arrancar.

## Por qué se está cambiando

`_run_pending_migrations` funciona, pero tiene tres problemas que no se arreglan
con cuidado:

1. **No sabe en qué versión está la base.** Cada arranque vuelve a revisar los
   49 cambios. Contra Render eso es la demora que se siente al correr cualquier
   script contra producción.
2. **No se puede revertir.** No hay `downgrade`. Si un cambio sale mal, se
   deshace a mano, en producción, con el CRM arriba.
3. **Cada bloque traga sus propios errores** (`except: log y sigue`). La app
   arranca igual con el esquema a medias, y el aviso queda en un log que nadie
   lee.

## Lo que ya quedó

El baseline (`migraciones/versions/20261001_*_esquema_base.py`) construye las
**69 tablas desde cero**, comprobado: se aplicó sobre una base vacía y la
comparación contra `models.py` dio **0 diferencias**. Eso antes no existía —
no había forma de levantar el esquema sin copiar una base.

Para lograrlo hubo que resolver dos cosas:

**El ciclo `users_crm` ↔ `usuarios`.** Las dos tablas se apuntan mutuamente, así
que no hay ningún orden válido para crearlas con sus llaves foráneas en línea.
El autogenerate avisa (`unresolvable cycles`) y emite un orden que falla. La FK
de `users_crm` se agrega con `ALTER` al final. **Si se regenera el baseline hay
que volver a moverla.**

**Dos índices que solo existían en el código de arranque.**
`ux_cs_invoices_savio_invoice_id` y `ux_cs_invoices_import_key` son UNIQUE
parciales creados por `_run_pending_migrations` y no estaban declarados en
`models.py` — las columnas pedían índice, no unicidad. Cualquier herramienta que
compare el modelo contra la base los lee como sobrantes y propone borrarlos, y
borrarlos reabre lo que evitan: duplicar facturas de Savio (rompe el MRR) y
duplicar cobros al re-subir el mismo CSV. Ya están en el modelo.

## Deriva pendiente

Quedan **4 índices en la base que no están en los modelos**, todos duplicados
reales de uno que sí está declarado:

| Sobrante | Ya cubierto por |
|---|---|
| `ix_cierre_evidencias_lead` | `ix_cierre_evidencias_lead_id` |
| `ix_cierre_evidencias_opp` | `ix_cierre_evidencias_oportunidad_id` |
| `ix_cs_accounts_dd` | `ix_cs_accounts_en_due_diligence` |
| `ux_sales_opportunity_id` | `ix_sales_opportunity_id` (UNIQUE, del modelo) |

Son inofensivos: cuestan un poco de escritura y nada más. Se borran en la
primera migración de verdad, no en el baseline.

## Cómo se usa

Los comandos necesitan `DATABASE_URL`; no está en `alembic.ini` a propósito,
para no dejar la credencial de producción versionada.

```bash
# en qué versión está la base
DATABASE_URL=$(grep '^DATABASE_URL=' .env.local | cut -d= -f2-) \
  python3 -m alembic current

# generar una migración después de cambiar models.py
DATABASE_URL=$(grep '^DATABASE_URL=' .env.local | cut -d= -f2-) \
  python3 -m alembic revision --autogenerate -m "lo que cambia"

# aplicarla
DATABASE_URL=$(grep '^DATABASE_URL=' .env.local | cut -d= -f2-) \
  python3 -m alembic upgrade head
```

**Lee siempre la migración generada antes de aplicarla.** El autogenerate no
ve índices parciales ni ciclos, y propone borrar lo que no entiende.

### Una base que ya tiene el esquema

`stamp` la marca como actualizada sin ejecutar nada:

```bash
DATABASE_URL=... python3 -m alembic stamp head
```

La base local ya está marcada. **Producción no.** Ese es el siguiente paso y es
el único con riesgo real.

## Lo que falta

1. `alembic stamp head` en producción — no cambia el esquema, solo escribe la
   tabla `alembic_version`.
2. Primera migración de verdad: borrar los 4 índices duplicados.
3. Ir vaciando `_run_pending_migrations`: lo que ya está aplicado en todas las
   bases se borra del arranque. Bloque por bloque, no de golpe.
4. Cuando quede vacío, quitar la llamada y el arranque deja de tocar el esquema.

El orden importa. Quitar los bloques antes de que producción esté marcada
dejaría una base sin el cambio y sin nadie que lo aplique.

#!/usr/bin/env python3
"""
Migración idempotente: agrega columna `client_id` a `accounts` y
asigna IDs secuenciales EMP-0001, EMP-0002, ... a las empresas existentes
ordenadas por fecha_creacion.

Uso:
  cd /Users/diego/Downloads/Carpetas/CRM
  python3 _migrate_account_client_id.py [--dry]

Es seguro correrlo más de una vez:
  - ADD COLUMN usa "IF NOT EXISTS"
  - Solo backfillea rows que tengan client_id IS NULL
"""
import argparse
import logging
import os
import sys

from dotenv import load_dotenv
load_dotenv()

from sqlalchemy import create_engine, text

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("migrate_client_id")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry", action="store_true", help="No commitea, solo reporta")
    args = parser.parse_args()

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        log.error("DATABASE_URL no seteada")
        sys.exit(1)

    engine = create_engine(db_url, pool_pre_ping=True)

    with engine.begin() as conn:
        # 1) ADD COLUMN si no existe
        log.info("Verificando si la columna client_id existe en accounts...")
        col_exists = conn.execute(text("""
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'accounts' AND column_name = 'client_id'
        """)).first()

        if col_exists:
            log.info("Columna client_id ya existe. Saltando ALTER.")
        else:
            log.info("Agregando columna client_id VARCHAR(10) UNIQUE INDEX...")
            if args.dry:
                log.info("[DRY] ALTER TABLE accounts ADD COLUMN client_id VARCHAR(10)")
            else:
                conn.execute(text(
                    "ALTER TABLE accounts ADD COLUMN client_id VARCHAR(10)"
                ))
                conn.execute(text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS ix_accounts_client_id "
                    "ON accounts (client_id)"
                ))
                log.info("Columna agregada.")

        # 2) Backfill: asigna EMP-XXXX a rows con NULL
        log.info("Buscando rows sin client_id...")
        rows = conn.execute(text("""
            SELECT id FROM accounts
            WHERE client_id IS NULL
            ORDER BY fecha_creacion ASC NULLS LAST, nombre ASC
        """)).fetchall()
        log.info("Rows sin client_id: %d", len(rows))

        if not rows:
            log.info("Nada que backfillear. Listo.")
            return

        # Encontrar el siguiente número disponible
        max_existing = conn.execute(text(
            "SELECT MAX(client_id) FROM accounts WHERE client_id LIKE 'EMP-%'"
        )).scalar()
        next_num = 1
        if max_existing:
            try:
                next_num = int(max_existing.split("-")[1]) + 1
            except (IndexError, ValueError):
                next_num = 1

        log.info("Empezando desde EMP-%04d", next_num)

        for row in rows:
            account_id = row[0]
            new_client_id = f"EMP-{next_num:04d}"
            if args.dry:
                log.info("[DRY] %s → %s", account_id, new_client_id)
            else:
                conn.execute(
                    text("UPDATE accounts SET client_id = :cid WHERE id = :id"),
                    {"cid": new_client_id, "id": account_id},
                )
            next_num += 1

        log.info("Backfill completado. Total: %d empresas.", len(rows))


if __name__ == "__main__":
    main()

"""
ETL: Zoho Analytics → Supabase (cs_appointments)

Reemplaza la carga manual de CSV de Citas/Operación. Diseñado para correr
on-demand (`python3 zoho_appointments_etl.py`) o como job del scheduler.

Variables de entorno requeridas (configurar en Render → Environment):
    ZOHO_CLIENT_ID
    ZOHO_CLIENT_SECRET
    ZOHO_REFRESH_TOKEN
    ZOHO_ACCOUNTS_DOMAIN     (default: accounts.zoho.com — .eu/.in si aplica)
    ZOHO_USER_EMAIL          (email del owner del workspace)
    ZOHO_WORKSPACE           (nombre exacto del workspace)
    ZOHO_TABLE               (nombre exacto de la tabla/vista)
    SUPABASE_URL             (https://<project>.supabase.co)
    SUPABASE_SERVICE_KEY     (service_role key — NUNCA exponer al cliente)

Prerequisito SQL (correr una vez en Supabase SQL Editor):
    ALTER TABLE cs_appointments
        ADD COLUMN IF NOT EXISTS zoho_appointment_id VARCHAR(64);
    CREATE UNIQUE INDEX IF NOT EXISTS ux_cs_appointments_zoho_id
        ON cs_appointments(zoho_appointment_id)
        WHERE zoho_appointment_id IS NOT NULL;
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import datetime
from typing import Any

import requests
from supabase import create_client, Client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("zoho_etl")

# ──────────────────────────────────────────────────────────────────
# Config — mapeo de columnas Zoho → Supabase (cs_appointments)
# Editá este dict si los headers en tu tabla de Zoho difieren.
# Las keys son los nombres en Zoho; los values son las columnas Supabase.
# ──────────────────────────────────────────────────────────────────
COLUMN_MAP: dict[str, str] = {
    "Appointment ID": "zoho_appointment_id",   # PK natural — clave de upsert
    "ID":              "zoho_appointment_id",   # fallback si el header se llama "ID"
    "Cliente":         "_cliente_lookup",       # NO es columna destino — se usa para resolver account_id
    "ID Cliente":      "_client_id_lookup",
    "Propiedad":       "propiedad",
    "Dirección":       "direccion",
    "Direccion":       "direccion",
    "Zona":            "zona",
    "Tecnico":         "tecnico",
    "Técnico":         "tecnico",
    "Fecha de Inicio": "fecha_inicio",
    "Fecha de Terminación": "fecha_terminacion",
    "Fecha de Terminacion": "fecha_terminacion",
    "Estatus":         "estatus",
    "Titulo Servicio": "titulo_servicio",
    "Título Servicio": "titulo_servicio",
    "Cantidad":        "cantidad",
}

DEST_TABLE = "cs_appointments"
ON_CONFLICT = "zoho_appointment_id"


# ──────────────────────────────────────────────────────────────────
# Step 1 — OAuth: refresh token → access token
# ──────────────────────────────────────────────────────────────────
def get_access_token() -> str:
    domain = os.getenv("ZOHO_ACCOUNTS_DOMAIN", "accounts.zoho.com")
    resp = requests.post(
        f"https://{domain}/oauth/v2/token",
        params={
            "refresh_token": os.environ["ZOHO_REFRESH_TOKEN"],
            "client_id":     os.environ["ZOHO_CLIENT_ID"],
            "client_secret": os.environ["ZOHO_CLIENT_SECRET"],
            "grant_type":    "refresh_token",
        },
        timeout=30,
    )
    resp.raise_for_status()
    body = resp.json()
    token = body.get("access_token")
    if not token:
        raise RuntimeError(f"Zoho token error: {body}")
    log.info("access_token OK (expira en %ss)", body.get("expires_in"))
    return token


# ──────────────────────────────────────────────────────────────────
# Step 2 — Export tabla Zoho Analytics en JSON
# Usa el endpoint /api clásico con ZOHO_ACTION=EXPORT y ZOHO_OUTPUT_FORMAT=JSON.
# Si tu cuenta usa el endpoint v2 /restapi/v2/, ajustá `fetch_table_v2`.
# ──────────────────────────────────────────────────────────────────
def fetch_table(access_token: str) -> list[dict[str, Any]]:
    user_email = os.environ["ZOHO_USER_EMAIL"]
    workspace  = os.environ["ZOHO_WORKSPACE"]
    table      = os.environ["ZOHO_TABLE"]

    url = f"https://analyticsapi.zoho.com/api/{user_email}/{workspace}/{table}"
    headers = {"Authorization": f"Zoho-oauthtoken {access_token}"}
    params = {
        "ZOHO_ACTION":        "EXPORT",
        "ZOHO_OUTPUT_FORMAT": "JSON",
        "ZOHO_ERROR_FORMAT":  "JSON",
        "ZOHO_API_VERSION":   "1.0",
    }

    log.info("export → %s", url)
    resp = requests.get(url, headers=headers, params=params, timeout=120)
    resp.raise_for_status()
    payload = resp.json()

    # Zoho v1 EXPORT JSON shape: {"response": {"result": {"column_order": [...], "rows": [[...], ...]}}}
    try:
        result = payload["response"]["result"]
        cols = result["column_order"]
        rows = result["rows"]
        records = [dict(zip(cols, r)) for r in rows]
    except KeyError:
        # Algunas variantes devuelven una lista plana de dicts
        records = payload if isinstance(payload, list) else payload.get("data", [])

    log.info("filas crudas extraídas: %d", len(records))
    return records


# ──────────────────────────────────────────────────────────────────
# Step 3 — Transformación
# ──────────────────────────────────────────────────────────────────
def _parse_dt(raw: Any) -> str | None:
    if not raw:
        return None
    s = str(raw).strip()
    if not s:
        return None
    for fmt in (
        "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f",
        "%d/%m/%Y %H:%M", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y", "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(s, fmt).isoformat()
        except ValueError:
            continue
    log.warning("fecha no parseada: %r", s)
    return None


def _to_int(raw: Any, default: int = 1) -> int:
    try:
        return int(float(raw)) if raw not in (None, "", "nan") else default
    except (ValueError, TypeError):
        return default


def transform(rows: list[dict[str, Any]], accounts_index: dict[str, str]) -> list[dict[str, Any]]:
    """Aplica COLUMN_MAP, resuelve account_id por nombre o ID de cliente, parsea fechas."""
    out: list[dict[str, Any]] = []
    sin_match = 0

    for r in rows:
        record: dict[str, Any] = {}
        cliente_lookup = ""
        client_id_lookup = ""

        for zoho_col, supabase_col in COLUMN_MAP.items():
            if zoho_col not in r:
                continue
            val = r[zoho_col]
            if supabase_col == "_cliente_lookup":
                cliente_lookup = (val or "").strip()
            elif supabase_col == "_client_id_lookup":
                client_id_lookup = (val or "").strip().upper()
            elif supabase_col.startswith("fecha_"):
                record[supabase_col] = _parse_dt(val)
            elif supabase_col == "cantidad":
                record[supabase_col] = _to_int(val, 1)
            else:
                record[supabase_col] = ("" if val is None else str(val)).strip()

        # Resolver account_id (client_id primero, luego nombre)
        acc_id = accounts_index.get(client_id_lookup) or accounts_index.get(cliente_lookup.lower())
        if not acc_id:
            sin_match += 1
            continue
        record["account_id"] = acc_id

        if not record.get(ON_CONFLICT):
            log.warning("fila sin %s → skip", ON_CONFLICT)
            continue

        out.append(record)

    if sin_match:
        log.warning("filas descartadas por cliente no encontrado: %d", sin_match)
    log.info("filas transformadas listas para upsert: %d", len(out))
    return out


# ──────────────────────────────────────────────────────────────────
# Step 4 — Upsert a Supabase
# ──────────────────────────────────────────────────────────────────
def get_supabase() -> Client:
    return create_client(
        os.environ["SUPABASE_URL"],
        os.environ["SUPABASE_SERVICE_KEY"],
    )


def fetch_accounts_index(sb: Client) -> dict[str, str]:
    """Construye {client_id_upper: account_id, nombre_lower: account_id} para resolver matches."""
    resp = sb.table("cs_accounts").select("id, nombre, client_id").execute()
    idx: dict[str, str] = {}
    for a in resp.data or []:
        if a.get("client_id"):
            idx[a["client_id"].upper()] = a["id"]
        if a.get("nombre"):
            idx[a["nombre"].lower()] = a["id"]
    log.info("cs_accounts indexados: %d entradas", len(idx))
    return idx


def upsert_batch(sb: Client, rows: list[dict[str, Any]], batch_size: int = 500) -> int:
    """Upsert paginado. Devuelve total insertado/actualizado."""
    total = 0
    for i in range(0, len(rows), batch_size):
        chunk = rows[i:i + batch_size]
        sb.table(DEST_TABLE).upsert(chunk, on_conflict=ON_CONFLICT).execute()
        total += len(chunk)
        log.info("upsert chunk %d-%d (%d filas) OK", i, i + len(chunk), len(chunk))
    return total


# ──────────────────────────────────────────────────────────────────
# Orquestador
# ──────────────────────────────────────────────────────────────────
def run() -> dict[str, Any]:
    t0 = time.time()
    log.info("=== Zoho → Supabase ETL · %s ===", datetime.utcnow().isoformat())

    token = get_access_token()
    raw_rows = fetch_table(token)

    sb = get_supabase()
    accounts_index = fetch_accounts_index(sb)

    transformed = transform(raw_rows, accounts_index)
    upserted = upsert_batch(sb, transformed) if transformed else 0

    elapsed = round(time.time() - t0, 1)
    summary = {
        "rows_zoho": len(raw_rows),
        "rows_transformed": len(transformed),
        "rows_upserted": upserted,
        "elapsed_s": elapsed,
    }
    log.info("=== fin · %s ===", json.dumps(summary))
    return summary


if __name__ == "__main__":
    try:
        run()
    except KeyError as e:
        log.error("falta env var: %s", e)
        sys.exit(2)
    except Exception as e:
        log.exception("ETL failed: %s", e)
        sys.exit(1)

#!/usr/bin/env python3
"""
Migración one-shot: Zoho CRM Deals → leads-manager Oportunidades.

Pulla todos los deals de Zoho via API REST (paginated), mapea Stage →
EtapaOportunidad, idempotente por zoho_deal_id. Crea Accounts y
Contacts auto si no existen.

Uso:
  cd /Users/diego/Downloads/CRM
  python3 _migrate_zoho_deals.py [--dry] [--only-open] [--limit N]

Args:
  --dry        Simula sin insertar
  --only-open  Solo deals que NO están cerrados (Closed Won / Closed Lost)
  --limit N    Procesa solo primeros N deals (para test)

Requiere:
  - Zoho OAuth conectado (zoho_tokens.refresh_token poblado en DB)
  - Fase 3 ALTER ya corrió (leads/oportunidades tienen account_id, etc)
"""
import argparse
import logging
import os
import sys
import time
import uuid
from datetime import date, datetime, timedelta, timezone

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("migrate_zoho")


# Zoho Stage → EtapaOportunidad (string del enum)
STAGE_MAP = {
    "Qualification":             "Calificación",
    "Needs Analysis":            "Análisis",
    "Identify Decision Makers":  "Análisis",
    "Value Proposition":         "Propuesta",
    "Proposal/Price Quote":      "Propuesta",
    "Negotiation/Review":        "Negociación",
    "Closed Won":                "Cerrado Ganado",
    "Closed Lost":               "Cerrado Perdido",
    "Closed Lost to Competition": "Cerrado Perdido",
    "Closed-Lost":               "Cerrado Perdido",  # variantes con guión
    "Closed-Won":                "Cerrado Ganado",
}

CLOSED_STAGES_ZOHO = {"Closed Won", "Closed Lost", "Closed Lost to Competition",
                       "Closed-Won", "Closed-Lost"}


def _parse_date(s):
    if not s:
        return None
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _parse_dt(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError, AttributeError):
        return None


# ── Zoho OAuth (standalone, lee tokens de DB) ─────────────────────


ZOHO_ACCOUNTS = "https://accounts.zoho.com"
ZOHO_API = "https://www.zohoapis.com/crm/v2"


def _get_access_token(pg_engine):
    """Lee tokens de DB. Si expirado, refresca y guarda."""
    import requests
    from sqlalchemy import text

    with pg_engine.connect() as conn:
        row = conn.execute(text(
            "SELECT id, access_token, refresh_token, expires_at FROM zoho_tokens LIMIT 1"
        )).first()
    if not row:
        raise RuntimeError("Zoho OAuth no conectado. Andá a /api/zoho/connect primero.")

    expires_at = row.expires_at
    now = datetime.now(timezone.utc)
    if expires_at and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    # Si quedan más de 60s, usar access_token actual
    if expires_at and (expires_at - timedelta(seconds=60)) > now:
        return row.access_token

    # Refresh
    log.info("Refrescando access_token de Zoho...")
    resp = requests.post(
        f"{ZOHO_ACCOUNTS}/oauth/v2/token",
        data={
            "grant_type": "refresh_token",
            "client_id": os.getenv("ZOHO_CLIENT_ID"),
            "client_secret": os.getenv("ZOHO_CLIENT_SECRET"),
            "refresh_token": row.refresh_token,
        }, timeout=20,
    )
    data = resp.json()
    if not data.get("access_token"):
        raise RuntimeError(f"Refresh falló: {data}")
    new_token = data["access_token"]
    new_expires = now + timedelta(seconds=int(data.get("expires_in") or 3600))
    with pg_engine.begin() as conn:
        conn.execute(text(
            "UPDATE zoho_tokens SET access_token=:t, expires_at=:e, updated_at=:u WHERE id=:i"
        ), {"t": new_token, "e": new_expires, "u": now, "i": row.id})
    return new_token


def _zoho_get(path, token):
    import requests
    resp = requests.get(
        f"{ZOHO_API}{path}",
        headers={"Authorization": f"Zoho-oauthtoken {token}"},
        timeout=30,
    )
    if resp.status_code == 204:
        return {}
    if resp.status_code >= 400:
        raise RuntimeError(f"Zoho GET {path} -> {resp.status_code}: {resp.text[:300]}")
    return resp.json()


# ── Main ───────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry", action="store_true")
    parser.add_argument("--only-open", action="store_true",
                        help="Skip Closed Won/Lost (default: trae todos)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Procesa solo N deals (test)")
    args = parser.parse_args()

    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        log.error("DATABASE_URL no configurada en .env")
        sys.exit(1)

    from sqlalchemy import create_engine, text
    pg = create_engine(db_url)

    log.info(f"=== MIGRANDO ZOHO DEALS → OPORTUNIDADES ===")
    log.info(f"Modo: {'DRY-RUN' if args.dry else 'EJECUTAR'}")
    if args.only_open:
        log.info(f"Filtro: solo deals NO cerrados")
    if args.limit:
        log.info(f"Limit: {args.limit} deals")

    # Get token
    try:
        token = _get_access_token(pg)
    except Exception as e:
        log.error(f"Token error: {e}")
        sys.exit(1)
    log.info("Token Zoho OK")

    # User email → leads-manager Usuario UUID
    log.info("Cargando mapa de usuarios...")
    email_to_usuario_id = {}
    with pg.connect() as conn:
        for correo, uid in conn.execute(text(
            "SELECT correo, usuario_id FROM users_crm WHERE usuario_id IS NOT NULL"
        )).fetchall():
            email_to_usuario_id[correo.lower()] = str(uid)
    log.info(f"  {len(email_to_usuario_id)} usuarios linkeados")

    # Existing zoho_deal_ids (dedup)
    with pg.connect() as conn:
        existing_zoho_ids = {r[0] for r in conn.execute(text(
            "SELECT zoho_deal_id FROM oportunidades WHERE zoho_deal_id IS NOT NULL"
        )).fetchall()}
    log.info(f"  {len(existing_zoho_ids)} oportunidades ya migradas (dedup)")

    # Cargar accounts existentes para reusar
    with pg.connect() as conn:
        # Por nombre
        name_to_acc = {}
        for r in conn.execute(text("SELECT id, nombre FROM accounts")).fetchall():
            name_to_acc[(r.nombre or "").lower().strip()] = str(r.id)
        # Por zoho_account_id
        zoho_to_acc = {}
        for r in conn.execute(text(
            "SELECT id, zoho_account_id FROM accounts WHERE zoho_account_id IS NOT NULL"
        )).fetchall():
            zoho_to_acc[r.zoho_account_id] = str(r.id)

    # Pull deals paginated
    log.info("\nPulling deals de Zoho...")
    all_deals = []
    page = 1
    while True:
        try:
            data = _zoho_get(f"/Deals?page={page}&per_page=200", token)
        except Exception as e:
            log.error(f"Pull deals page {page}: {e}")
            break
        deals = data.get("data") or []
        if not deals:
            break
        all_deals.extend(deals)
        log.info(f"  Página {page}: {len(deals)} deals (acumulado {len(all_deals)})")
        info = data.get("info") or {}
        if not info.get("more_records"):
            break
        page += 1
        time.sleep(0.6)  # rate limit politely
        if args.limit and len(all_deals) >= args.limit:
            all_deals = all_deals[:args.limit]
            break
    log.info(f"Total deals pulled: {len(all_deals)}")

    # Procesar
    inserted = 0
    skipped_existing = 0
    skipped_closed = 0
    accounts_created = 0
    errors = 0
    sample_errors = []

    inserts_opp = []
    inserts_acc = []
    for deal in all_deals:
        try:
            zoho_id = str(deal.get("id"))
            if zoho_id in existing_zoho_ids:
                skipped_existing += 1
                continue
            stage = (deal.get("Stage") or "").strip()
            if args.only_open and stage in CLOSED_STAGES_ZOHO:
                skipped_closed += 1
                continue
            etapa = STAGE_MAP.get(stage) or "Calificación"

            # Account: Zoho Deal.Account_Name es {id, name} lookup
            acc_block = deal.get("Account_Name") or {}
            acc_name = (acc_block.get("name") or "").strip() if isinstance(acc_block, dict) else str(acc_block or "").strip()
            zoho_acc_id = (acc_block.get("id") if isinstance(acc_block, dict) else None)

            account_id = None
            if zoho_acc_id and zoho_acc_id in zoho_to_acc:
                account_id = zoho_to_acc[zoho_acc_id]
            elif acc_name and acc_name.lower() in name_to_acc:
                account_id = name_to_acc[acc_name.lower()]
            elif acc_name:
                # Crear nueva account
                new_acc_id = str(uuid.uuid4())
                inserts_acc.append({
                    "id": new_acc_id, "nombre": acc_name, "rfc": None,
                    "zoho_account_id": zoho_acc_id,
                    "is_cliente": (stage == "Closed Won"),
                })
                name_to_acc[acc_name.lower()] = new_acc_id
                if zoho_acc_id:
                    zoho_to_acc[zoho_acc_id] = new_acc_id
                account_id = new_acc_id
                accounts_created += 1

            # Contact: Zoho Deal.Contact_Name → buscar email/teléfono no expuestos sin extra call
            # Por ahora dejamos contact_id en None; usuario puede vincular después

            # Owner → propietario
            owner_block = deal.get("Owner") or {}
            owner_email = (owner_block.get("email") or "").lower() if isinstance(owner_block, dict) else ""
            propietario_id = email_to_usuario_id.get(owner_email)

            # Valor + fechas + marca
            amount = deal.get("Amount") or 0
            try:
                amount = float(amount)
            except (ValueError, TypeError):
                amount = 0
            closing_date = _parse_date(deal.get("Closing_Date"))
            created_at = _parse_dt(deal.get("Created_Time")) or datetime.now(timezone.utc)

            # Marca via Lead_Source o Type heurística
            marca = None
            lsrc = (deal.get("Lead_Source") or "").lower()
            if "aromatex" in lsrc:
                marca = "Aromatex"
            elif "pestex" in lsrc:
                marca = "Pestex"
            elif "weldex" in lsrc:
                marca = "Weldex"

            probability = deal.get("Probability") or 10
            try:
                probability = max(0, min(100, int(probability)))
            except (ValueError, TypeError):
                probability = 10

            nombre = (deal.get("Deal_Name") or "").strip() or f"Zoho {zoho_id}"

            inserts_opp.append({
                "id": str(uuid.uuid4()),
                "nombre": nombre,
                "empresa": acc_name or None,
                "contacto_nombre": None,
                "contacto_telefono": None,
                "contacto_email": None,
                "valor": amount, "moneda": "MXN",
                "fecha_cierre_esperada": closing_date,
                "etapa": etapa, "probabilidad": probability,
                "propietario_id": propietario_id,
                "marca_interes": marca,
                "estado_cliente": None,
                "num_sucursales": None,
                "monthly_amount": None,
                "sale_type": (deal.get("Type") or None),
                "notas": (deal.get("Description") or None),
                "motivo_perdida": None,
                "lead_id": None,
                "account_id": account_id,
                "contact_id": None,
                "zoho_deal_id": zoho_id,
                "fecha_creacion": created_at,
                "fecha_actualizacion": created_at,
                "fecha_cierre_real": (closing_date if stage in CLOSED_STAGES_ZOHO else None),
            })
        except Exception as e:
            errors += 1
            if len(sample_errors) < 5:
                sample_errors.append(f"  zoho_id={deal.get('id')}: {e}")

    # Bulk insert
    if not args.dry:
        if inserts_acc:
            log.info(f"\nInsertando {len(inserts_acc)} accounts nuevos...")
            with pg.begin() as conn:
                for i in range(0, len(inserts_acc), 200):
                    batch = inserts_acc[i:i+200]
                    conn.execute(text("""
                        INSERT INTO accounts (id, nombre, rfc, zoho_account_id, is_cliente)
                        VALUES (:id, :nombre, :rfc, :zoho_account_id, :is_cliente)
                        ON CONFLICT (nombre) DO NOTHING
                    """), batch)

        if inserts_opp:
            log.info(f"\nInsertando {len(inserts_opp)} oportunidades en lotes de 200...")
            with pg.begin() as conn:
                for i in range(0, len(inserts_opp), 200):
                    batch = inserts_opp[i:i+200]
                    conn.execute(text("""
                        INSERT INTO oportunidades (
                            id, nombre, empresa, contacto_nombre, contacto_telefono,
                            contacto_email, valor, moneda, fecha_cierre_esperada,
                            etapa, probabilidad, propietario_id, marca_interes,
                            estado_cliente, num_sucursales, monthly_amount, sale_type,
                            notas, motivo_perdida, lead_id, account_id, contact_id,
                            zoho_deal_id, fecha_creacion, fecha_actualizacion, fecha_cierre_real
                        ) VALUES (
                            :id, :nombre, :empresa, :contacto_nombre, :contacto_telefono,
                            :contacto_email, :valor, :moneda, :fecha_cierre_esperada,
                            :etapa, :probabilidad, :propietario_id, :marca_interes,
                            :estado_cliente, :num_sucursales, :monthly_amount, :sale_type,
                            :notas, :motivo_perdida, :lead_id, :account_id, :contact_id,
                            :zoho_deal_id, :fecha_creacion, :fecha_actualizacion, :fecha_cierre_real
                        )
                        ON CONFLICT (zoho_deal_id) DO NOTHING
                    """), batch)
                    inserted += len(batch)
                    log.info(f"  Lote {i//200+1}: {len(batch)} insertados")

    # Resumen
    log.info("\n=== RESUMEN ===")
    log.info(f"Deals pulled de Zoho:         {len(all_deals)}")
    log.info(f"Ya existían (dedup):           {skipped_existing}")
    if args.only_open:
        log.info(f"Skip cerrados (--only-open):   {skipped_closed}")
    log.info(f"Accounts {'a crear' if args.dry else 'creados'}:           {accounts_created}")
    log.info(f"Oportunidades {'a insertar' if args.dry else 'insertadas'}:    {len(inserts_opp) if args.dry else inserted}")
    log.info(f"Errores transformación:        {errors}")
    if sample_errors:
        log.info("\nSample errores:")
        for e in sample_errors:
            log.info(e)
    if not args.dry:
        log.info(f"\nMigración completa. Ver en /api/oportunidades/ o sidebar Oportunidades.")


if __name__ == "__main__":
    main()

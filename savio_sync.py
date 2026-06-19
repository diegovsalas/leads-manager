"""
Sincronización Savio → DB local.

Port de los sync jobs de vendedores.cloud/savio.js. Llama a savio_client,
clasifica con savio_classifier, y hace upsert en SavioCustomer/Subscription/
Invoice/Payment. Auto-crea CustomerMaster/CustomerRfc para customers nuevos.

UEN se extrae de `record.custom_fields[]` filtrando por `custom_field_id == 235`
(constante de la API de Savio).

Pipeline:
  sync_invoices    → últimos 90d (o month=YYYY-MM); usa cursor pagination
  sync_payments    → últimos 90d (o month=YYYY-MM); cursor pagination
  sync_subscriptions → todas; page pagination
  sync_customers   → todas (lista plana); luego deriva unit + auto-crea masters
  bridge_savio_to_cs_mrr → matchea SavioCustomer.tax_id con CSAccount y suma MRR

Diseño: idempotente. Cada sync se puede correr varias veces sin duplicar.
"""

import os
import sys
import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from extensions import db
from models import (
    SavioCustomer, SavioSubscription, SavioInvoice, SavioPayment,
    CustomerMaster, CustomerRfc, CSAccount, CSInvoice,
)
import savio_client
from savio_classifier import classify_subscription

log = logging.getLogger("savio_sync")

UEN_CUSTOM_FIELD_ID = 235

# Ventana por defecto para sync incremental (invoices/payments). Antes 90d;
# se amplió a 365d para que facturas viejas con pagos tardíos se refresquen.
# Override via env: SAVIO_SYNC_WINDOW_DAYS.
DEFAULT_SYNC_WINDOW_DAYS = int(os.getenv("SAVIO_SYNC_WINDOW_DAYS", "365"))


def _extract_uen(record: dict) -> Optional[str]:
    """Saca el UEN del array custom_fields del record."""
    fields = record.get("custom_fields")
    if not isinstance(fields, list):
        return None
    for f in fields:
        if f.get("custom_field_id") == UEN_CUSTOM_FIELD_ID:
            return f.get("value")
    return None


def _parse_date(value) -> Optional[date]:
    if not value:
        return None
    s = str(value)[:10]
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def _date_range_from(month: Optional[str], fallback_days: int = DEFAULT_SYNC_WINDOW_DAYS) -> tuple[str, str]:
    """Devuelve (start_date, end_date) en formato YYYY-MM-DD.
    Si month='YYYY-MM' usa el mes completo; si no, los últimos `fallback_days`."""
    if month and len(month) == 7 and month[4] == "-":
        try:
            y, m = int(month[:4]), int(month[5:])
            start = date(y, m, 1)
            if m == 12:
                end = date(y + 1, 1, 1) - timedelta(days=1)
            else:
                end = date(y, m + 1, 1) - timedelta(days=1)
            return start.isoformat(), end.isoformat()
        except ValueError:
            pass
    today = datetime.now(timezone.utc).date()
    return (today - timedelta(days=fallback_days)).isoformat(), today.isoformat()


# ── Sync jobs ──────────────────────────────────────────────────────


def sync_customers() -> dict:
    """Trae todos los customers, hace upsert. Después deriva unit por dominante
    y auto-puebla customer_master/customer_rfcs."""
    rows = savio_client.list_customers()
    count = 0
    for c in rows:
        cid = str(c.get("customer_id"))
        addr = c.get("address") or {}
        stmt = pg_insert(SavioCustomer).values(
            customer_id=cid,
            name=c.get("name") or None,
            legal_name=c.get("legal_name") or None,
            tax_id=c.get("tax_id") or None,
            city=addr.get("city") or None,
            state=addr.get("state") or None,
            current_state=c.get("current_state") or None,
            raw_data=c,
        ).on_conflict_do_update(
            index_elements=[SavioCustomer.customer_id],
            set_={
                "name": c.get("name") or None,
                "legal_name": c.get("legal_name") or None,
                "tax_id": c.get("tax_id") or None,
                "city": addr.get("city") or None,
                "state": addr.get("state") or None,
                "current_state": c.get("current_state") or None,
                "raw_data": c,
                "updated_at": datetime.now(timezone.utc),
            },
        )
        db.session.execute(stmt)
        count += 1
    db.session.commit()
    derive_customer_units()
    masters_created = auto_populate_masters()
    return {"count": count, "masters_created": masters_created}


def derive_customer_units():
    """Setea SavioCustomer.unit con la unit dominante entre sus invoices+subs."""
    sql = text("""
        WITH u AS (
          SELECT customer_id, unit, COUNT(*) AS n FROM savio_invoices
          WHERE unit IS NOT NULL AND customer_id IS NOT NULL
          GROUP BY customer_id, unit
          UNION ALL
          SELECT customer_id, unit, COUNT(*) AS n FROM savio_subscriptions
          WHERE unit IS NOT NULL AND customer_id IS NOT NULL
          GROUP BY customer_id, unit
        ),
        agg AS (
          SELECT customer_id, unit, SUM(n) AS total,
                 ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY SUM(n) DESC) AS rn
          FROM u GROUP BY customer_id, unit
        )
        UPDATE savio_customers sc SET unit = agg.unit
        FROM agg
        WHERE agg.customer_id = sc.customer_id AND agg.rn = 1
    """)
    db.session.execute(sql)
    db.session.commit()


def auto_populate_masters() -> int:
    """Crea CustomerMaster + CustomerRfc para SavioCustomers no mapeados.
    Si el RFC ya existe en customer_rfcs, los agrega al mismo master.
    Devuelve cuántos masters nuevos se crearon.

    Defensivo: cada fila se inserta en su propio savepoint para que un
    UniqueViolation (ej. RFC genérico "XAXX010101000" compartido por muchos
    clientes Savio) no aborte el batch entero."""
    unmapped = (
        db.session.query(SavioCustomer)
        .filter(SavioCustomer.tax_id.isnot(None), SavioCustomer.tax_id != "")
        .filter(
            ~db.session.query(CustomerRfc.savio_customer_id)
            .filter(CustomerRfc.savio_customer_id == SavioCustomer.customer_id)
            .exists()
        )
        .all()
    )
    created = 0
    skipped = 0
    for c in unmapped:
        try:
            with db.session.begin_nested():  # savepoint por fila
                existing = CustomerRfc.query.filter_by(rfc=c.tax_id).first()
                if existing:
                    new_rfc = CustomerRfc(
                        master_id=existing.master_id,
                        rfc=c.tax_id,
                        legal_name=c.legal_name or "",
                        savio_customer_id=c.customer_id,
                    )
                    db.session.add(new_rfc)
                else:
                    master = CustomerMaster(master_name=c.name or c.legal_name or "")
                    db.session.add(master)
                    db.session.flush()
                    new_rfc = CustomerRfc(
                        master_id=master.id,
                        rfc=c.tax_id,
                        legal_name=c.legal_name or "",
                        savio_customer_id=c.customer_id,
                    )
                    db.session.add(new_rfc)
                    created += 1
        except Exception:
            skipped += 1
            # savepoint hace rollback solo de esta fila — la sesión sigue viva
    db.session.commit()
    if skipped:
        import logging as _logging
        _logging.warning("[savio] auto_populate_masters: %d filas saltadas por duplicados", skipped)
    return created


def sync_subscriptions() -> dict:
    """Page-paginated. Aplica classify_subscription para llenar unit/type/sub/sum_mrr."""
    today = datetime.now(timezone.utc).date().isoformat()
    count = 0
    for s in savio_client.list_subscriptions():
        sid = str(s.get("subscription_id"))
        uen = _extract_uen(s)
        cls = classify_subscription(uen, s.get("description"))
        contract_end = s.get("contract_end_date")
        status = "active" if (not contract_end or contract_end > today) else "ended"
        stmt = pg_insert(SavioSubscription).values(
            id=sid,
            customer_id=str(s.get("customer_id")) if s.get("customer_id") is not None else None,
            description=s.get("description") or None,
            amount=s.get("amount_total"),
            mrr=s.get("mrr"),
            status=status,
            start_date=_parse_date(s.get("start_date")),
            contract_end_date=_parse_date(contract_end),
            uen=uen,
            unit=cls["unit"],
            type=cls["type"],
            sum_mrr=bool(cls["sum_mrr"]),
            raw_data=s,
        ).on_conflict_do_update(
            index_elements=[SavioSubscription.id],
            set_={
                "customer_id": str(s.get("customer_id")) if s.get("customer_id") is not None else None,
                "description": s.get("description") or None,
                "amount": s.get("amount_total"),
                "mrr": s.get("mrr"),
                "status": status,
                "start_date": _parse_date(s.get("start_date")),
                "contract_end_date": _parse_date(contract_end),
                "uen": uen,
                "unit": cls["unit"],
                "type": cls["type"],
                "sum_mrr": bool(cls["sum_mrr"]),
                "raw_data": s,
                "updated_at": datetime.now(timezone.utc),
            },
        )
        db.session.execute(stmt)
        count += 1
        if count % 100 == 0:
            db.session.commit()
    db.session.commit()
    return {"count": count}


def sync_invoices(month: Optional[str] = None, days: Optional[int] = None) -> dict:
    """Cursor-paginated. Filtro por mes (YYYY-MM) o por `days` días (default DEFAULT_SYNC_WINDOW_DAYS)."""
    start_date, end_date = _date_range_from(month, fallback_days=days or DEFAULT_SYNC_WINDOW_DAYS)
    count = 0
    for i in savio_client.list_invoices(start_date=start_date, end_date=end_date):
        iid = str(i.get("invoice_id"))
        uen = _extract_uen(i)
        desc = i.get("description") or ""
        cls = classify_subscription(uen, desc)
        # invoice_date o cfdi_issuance_date[:10]
        inv_date = i.get("invoice_date")
        if not inv_date and i.get("cfdi_issuance_date"):
            inv_date = i["cfdi_issuance_date"][:10]
        stmt = pg_insert(SavioInvoice).values(
            id=iid,
            customer_id=str(i.get("customer_id")) if i.get("customer_id") is not None else None,
            customer_name=i.get("customer_display_name") or None,
            invoice_number=i.get("invoice_num") or None,
            amount=i.get("amount_total"),
            status=i.get("status") or None,
            date=_parse_date(inv_date),
            uen=uen,
            unit=cls["unit"],
            type=cls["type"],
            sum_mrr=bool(cls["sum_mrr"]),
            sub=cls.get("sub"),
            description=desc or None,
            raw_data=i,
        ).on_conflict_do_update(
            index_elements=[SavioInvoice.id],
            set_={
                "customer_id": str(i.get("customer_id")) if i.get("customer_id") is not None else None,
                "customer_name": i.get("customer_display_name") or None,
                "invoice_number": i.get("invoice_num") or None,
                "amount": i.get("amount_total"),
                "status": i.get("status") or None,
                "date": _parse_date(inv_date),
                "uen": uen,
                "unit": cls["unit"],
                "type": cls["type"],
                "sum_mrr": bool(cls["sum_mrr"]),
                "sub": cls.get("sub"),
                "description": desc or None,
                "raw_data": i,
                "updated_at": datetime.now(timezone.utc),
            },
        )
        db.session.execute(stmt)
        count += 1
        if count % 100 == 0:
            db.session.commit()
    db.session.commit()
    return {"count": count, "start_date": start_date, "end_date": end_date}


def sync_payments(month: Optional[str] = None, days: Optional[int] = None) -> dict:
    """Cursor-paginated. Un payment puede aplicar a varias invoices; guardamos
    la primera invoice_id (el monto total queda en la fila del payment)."""
    start_date, end_date = _date_range_from(month, fallback_days=days or DEFAULT_SYNC_WINDOW_DAYS)
    count = 0
    for p in savio_client.list_payments(start_date=start_date, end_date=end_date):
        pid = str(p.get("payment_id"))
        ips = p.get("invoice_payments") or []
        first_inv = str(ips[0].get("invoice_id")) if ips and ips[0].get("invoice_id") is not None else None
        stmt = pg_insert(SavioPayment).values(
            id=pid,
            invoice_id=first_inv,
            customer_id=str(p.get("customer_id")) if p.get("customer_id") is not None else None,
            amount=p.get("amount_paid"),
            date=_parse_date(p.get("payment_date")),
            method=p.get("payment_form") or None,
            raw_data=p,
        ).on_conflict_do_update(
            index_elements=[SavioPayment.id],
            set_={
                "invoice_id": first_inv,
                "customer_id": str(p.get("customer_id")) if p.get("customer_id") is not None else None,
                "amount": p.get("amount_paid"),
                "date": _parse_date(p.get("payment_date")),
                "method": p.get("payment_form") or None,
                "raw_data": p,
                "updated_at": datetime.now(timezone.utc),
            },
        )
        db.session.execute(stmt)
        count += 1
        if count % 100 == 0:
            db.session.commit()
    db.session.commit()
    return {"count": count, "start_date": start_date, "end_date": end_date}


# ── Bridge Savio → CSAccount ───────────────────────────────────────


def bridge_savio_to_cs_mrr() -> dict:
    """Actualiza CSAccount.mrr, CSAccount.mrr_observado y CSAccount.arr_proyectado.

    Dos métricas complementarias:
      mrr           = MRR contratado: suma de SavioSubscription.mrr activas
                      (sum_mrr=true) vinculadas vía CustomerMaster → CustomerRfc.
                      Refleja lo que Savio formalmente reconoce como recurrente.
      mrr_observado = MRR real: promedio mensual de facturación con UEN
                      RECURRENTE/POLIZAS de los últimos N meses (ventana de
                      cs_invoices). Captura el recurrente que el equipo
                      timbra factura por factura sin sub formal.

    El gap entre ambos identifica cuentas donde Savio está desactualizado
    operativamente (ej. Elektra factura $727k/mes pero solo tiene 1 sub
    registrada de $10k).

    El path para mrr es:
      CSAccount.id → CustomerMaster.cs_account_id → CustomerRfc.savio_customer_id
                  → SavioSubscription (filtrada por customer_id, sum_mrr, fechas)
    """
    today = datetime.now(timezone.utc).date()
    matched = 0
    no_master = 0
    no_subs = 0
    OBS_MONTHS = 5  # ventana para mrr_observado

    # ── Precalcular mrr_observado en bulk (una sola query) ─────────────
    from sqlalchemy import text as _sa_text
    obs_rows = db.session.execute(_sa_text("""
        SELECT
          account_id,
          ROUND(SUM(total) / :months, 2) AS mrr_obs
        FROM cs_invoices
        WHERE fecha_cobro >= (CURRENT_DATE - (:months || ' months')::interval)
          AND (uen ILIKE '%RECURRENTE%' OR uen ILIKE '%POLIZA%')
        GROUP BY account_id
    """), {"months": OBS_MONTHS}).fetchall()
    mrr_obs_map = {str(r[0]): Decimal(str(r[1] or 0)) for r in obs_rows}

    accounts = db.session.query(CSAccount).all()
    for acc in accounts:
        # mrr_observado (siempre, aunque no tenga master)
        if hasattr(acc, "mrr_observado"):
            acc.mrr_observado = mrr_obs_map.get(str(acc.id), Decimal("0"))

        # mrr contratado
        master = CustomerMaster.query.filter_by(cs_account_id=acc.id).first()
        if not master:
            no_master += 1
            if hasattr(acc, "mrr"):
                acc.mrr = Decimal("0")
            continue
        rfcs = CustomerRfc.query.filter_by(master_id=master.id).all()
        savio_cids = [r.savio_customer_id for r in rfcs if r.savio_customer_id]
        if not savio_cids:
            no_subs += 1
            if hasattr(acc, "mrr"):
                acc.mrr = Decimal("0")
            continue
        total = (
            db.session.query(db.func.coalesce(db.func.sum(SavioSubscription.mrr), 0))
            .filter(SavioSubscription.customer_id.in_(savio_cids))
            .filter(SavioSubscription.sum_mrr.is_(True))
            .filter(SavioSubscription.start_date <= today)
            .filter(
                (SavioSubscription.contract_end_date.is_(None))
                | (SavioSubscription.contract_end_date > today)
            )
            .scalar()
        )
        mrr_total = Decimal(str(total or 0))
        if hasattr(acc, "mrr"):
            acc.mrr = mrr_total
        if hasattr(acc, "arr_proyectado"):
            acc.arr_proyectado = mrr_total * Decimal("12")
        matched += 1
    db.session.commit()
    return {"matched": matched, "no_master": no_master, "no_subs": no_subs,
            "mrr_observado_accounts": len(mrr_obs_map)}


def sync_savio_to_cs_invoices(account_id: Optional[str] = None) -> dict:
    """Por cada CSAccount, busca su CustomerMaster (link cs_account_id),
    sus CustomerRfc, y de cada SavioCustomer asociado pulla TODAS las
    SavioInvoices y las upserta como CSInvoice. Idempotente por
    savio_invoice_id. Al final recalcula facturacion_q1/pagado_q1/
    pendiente_q1/num_facturas_q1 de los accounts tocados.

    Si account_id viene, solo procesa esa cuenta. Si no, procesa todas.
    """
    from decimal import Decimal as _D

    accounts_q = CSAccount.query
    if account_id:
        accounts_q = accounts_q.filter(CSAccount.id == account_id)
    accounts = accounts_q.all()

    summary = {
        "accounts_total": len(accounts),
        "accounts_synced": 0,
        "accounts_no_master": 0,
        "accounts_no_rfcs": 0,
        "invoices_inserted": 0,
        "invoices_updated": 0,
        "details": [],
    }
    touched_accounts = []
    for acc in accounts:
        master = CustomerMaster.query.filter_by(cs_account_id=acc.id).first()
        if not master:
            summary["accounts_no_master"] += 1
            summary["details"].append({"account": acc.nombre, "status": "no_master"})
            continue
        rfcs = CustomerRfc.query.filter_by(master_id=master.id).all()
        if not rfcs:
            summary["accounts_no_rfcs"] += 1
            summary["details"].append({"account": acc.nombre, "status": "no_rfcs"})
            continue
        savio_cids = [r.savio_customer_id for r in rfcs if r.savio_customer_id]
        if not savio_cids:
            summary["accounts_no_rfcs"] += 1
            summary["details"].append({"account": acc.nombre, "status": "no_savio_customer_ids"})
            continue

        # Pull todas las SavioInvoice locales para esos customers
        savio_invs = (
            SavioInvoice.query
            .filter(SavioInvoice.customer_id.in_(savio_cids))
            .all()
        )

        ins, upd = 0, 0
        for sv in savio_invs:
            try:
                sv_id = int(sv.id)
            except (ValueError, TypeError):
                continue
            existing = CSInvoice.query.filter_by(savio_invoice_id=sv_id).first()
            total = _D(str(float(sv.amount or 0)))
            # SavioInvoice no tiene amount_paid/remaining persistido, lo tomamos
            # del raw_data si existe (vino del API). Fallback: pagado=0.
            raw = sv.raw_data or {}
            pagado_v = raw.get("amount_paid")
            pend_v = raw.get("amount_remaining")
            pagado = _D(str(float(pagado_v))) if pagado_v is not None else _D("0")
            pendiente = _D(str(float(pend_v))) if pend_v is not None else (total - pagado)
            subtotal = (total / _D("1.16")).quantize(_D("0.01")) if total else _D("0")
            impuestos = total - subtotal
            settled = raw.get("settled_date")
            settled_d = _parse_date(settled[:10]) if settled else None
            due = raw.get("date_due")
            due_d = _parse_date(due) if due else None
            series = raw.get("series") or ""
            estatus = (raw.get("status") or sv.status or "").strip()

            if existing:
                existing.account_id = acc.id
                existing.folio = sv.invoice_number or ""
                existing.serie = series
                existing.concepto = (sv.description or "")[:300]
                existing.uen = (sv.uen or "")[:50]
                existing.subtotal = subtotal
                existing.impuestos = impuestos
                existing.total = total
                existing.pagado = pagado
                existing.pendiente = pendiente
                existing.fecha_cobro = sv.date
                existing.fecha_vencimiento = due_d
                existing.fecha_pago = settled_d
                existing.estatus = estatus[:30]
                upd += 1
            else:
                new_row = CSInvoice(
                    account_id=acc.id, folio=sv.invoice_number or "",
                    serie=series, concepto=(sv.description or "")[:300],
                    uen=(sv.uen or "")[:50],
                    subtotal=subtotal, impuestos=impuestos, total=total,
                    pagado=pagado, pendiente=pendiente,
                    fecha_cobro=sv.date, fecha_vencimiento=due_d,
                    fecha_pago=settled_d, estatus=estatus[:30],
                    savio_invoice_id=sv_id,
                )
                db.session.add(new_row)
                ins += 1
        db.session.commit()

        summary["invoices_inserted"] += ins
        summary["invoices_updated"] += upd
        summary["accounts_synced"] += 1
        summary["details"].append({
            "account": acc.nombre, "status": "ok",
            "savio_customers": len(savio_cids), "ins": ins, "upd": upd,
        })
        touched_accounts.append(acc)

    # Recalcular rollups en accounts tocadas
    if touched_accounts:
        for acc in touched_accounts:
            totals = (
                db.session.query(
                    db.func.coalesce(db.func.sum(CSInvoice.total), 0),
                    db.func.coalesce(db.func.sum(CSInvoice.pagado), 0),
                    db.func.coalesce(db.func.sum(CSInvoice.pendiente), 0),
                    db.func.count(CSInvoice.id),
                ).filter(CSInvoice.account_id == acc.id).first()
            )
            acc.facturacion_q1 = float(totals[0] or 0)
            acc.pagado_q1 = float(totals[1] or 0)
            acc.pendiente_q1 = float(totals[2] or 0)
            acc.num_facturas_q1 = int(totals[3] or 0)
        db.session.commit()

    return summary


def sync_all(month: Optional[str] = None, days: Optional[int] = None) -> dict:
    """Orquesta los syncs en orden seguro + propaga al espejo CS.
    No corta si uno falla. `days` override de ventana solo aplica a invoices+payments."""
    t0 = datetime.now(timezone.utc)
    result = {}
    for name, fn, args in [
        ("invoices",          sync_invoices,             (month, days)),
        ("payments",          sync_payments,             (month, days)),
        ("subscriptions",     sync_subscriptions,        ()),
        ("customers",         sync_customers,            ()),
        ("bridge_cs_mrr",     bridge_savio_to_cs_mrr,    ()),
        ("cs_invoices_mirror", sync_savio_to_cs_invoices, ()),
    ]:
        try:
            result[name] = fn(*args)
        except Exception as e:
            db.session.rollback()
            log.exception(f"savio_sync.{name} falló")
            result[name] = {"error": str(e)}
    result["elapsed_seconds"] = (datetime.now(timezone.utc) - t0).total_seconds()
    log.info(f"[SAVIO SYNC] {result}")
    return result


if __name__ == "__main__":
    """CLI: python3 savio_sync.py [all|customers|subscriptions|invoices|payments|bridge] [YYYY-MM]"""
    from dotenv import load_dotenv
    load_dotenv()
    from avantex_crm import create_app

    app = create_app()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
    month = sys.argv[2] if len(sys.argv) > 2 else None

    with app.app_context():
        try:
            if cmd == "all":
                out = sync_all(month)
            elif cmd == "customers":
                out = sync_customers()
            elif cmd == "subscriptions":
                out = sync_subscriptions()
            elif cmd == "invoices":
                out = sync_invoices(month)
            elif cmd == "payments":
                out = sync_payments(month)
            elif cmd == "bridge":
                out = bridge_savio_to_cs_mrr()
            else:
                print(f"comando desconocido: {cmd}", file=sys.stderr)
                sys.exit(2)
            import json
            print(json.dumps(out, indent=2, default=str, ensure_ascii=False))
        except Exception as e:
            log.exception("savio_sync CLI falló")
            print(f"[savio_sync] error: {e}", file=sys.stderr)
            sys.exit(1)

# blueprints/savio.py
"""
Savio — endpoints API.
Rutas bajo /api/savio/.

Lectura sobre las tablas savio_*. Trigger manual de sync.
Customer master CRUD. Webhook receiver con HMAC.
"""
import hashlib
import hmac
import os
from datetime import datetime, timezone
from flask import Blueprint, request, jsonify
from sqlalchemy import func, or_

from extensions import db
from models import (
    SavioCustomer, SavioSubscription, SavioInvoice, SavioPayment,
    CustomerMaster, CustomerRfc,
)
from savio_mrr import mrr_report
import savio_sync

savio_bp = Blueprint("savio", __name__)


def _parse_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def _parse_bool(value):
    if value is None:
        return None
    v = str(value).strip().lower()
    if v in ("true", "1", "yes", "y", "si"):
        return True
    if v in ("false", "0", "no", "n"):
        return False
    return None


# ── Customers ──────────────────────────────────────────────────────


@savio_bp.route("/customers", methods=["GET"])
def list_customers():
    """Filtros: ?unit= ?state= ?search= (matchea name/legal_name/tax_id)."""
    q = SavioCustomer.query
    unit = request.args.get("unit")
    state = request.args.get("state")
    search = request.args.get("search", "").strip()

    if unit:
        q = q.filter(SavioCustomer.unit == unit)
    if state:
        q = q.filter(SavioCustomer.current_state == state)
    if search:
        like = f"%{search}%"
        q = q.filter(or_(
            SavioCustomer.name.ilike(like),
            SavioCustomer.legal_name.ilike(like),
            SavioCustomer.tax_id.ilike(like),
        ))

    return jsonify([c.to_dict() for c in q.order_by(SavioCustomer.name.asc()).all()])


@savio_bp.route("/customers/<string:customer_id>", methods=["GET"])
def get_customer(customer_id):
    customer = db.session.get(SavioCustomer, customer_id)
    if not customer:
        return jsonify({"error": "Customer no encontrado"}), 404
    payload = customer.to_dict()
    payload["subscriptions"] = [s.to_dict() for s in customer.subscriptions]
    return jsonify(payload)


# ── Subscriptions ──────────────────────────────────────────────────


@savio_bp.route("/subscriptions", methods=["GET"])
def list_subscriptions():
    q = SavioSubscription.query
    unit = request.args.get("unit")
    type_ = request.args.get("type")
    customer = request.args.get("customer")
    status = request.args.get("status")
    sum_mrr = _parse_bool(request.args.get("sum_mrr"))

    if unit:
        q = q.filter(SavioSubscription.unit == unit)
    if type_:
        q = q.filter(SavioSubscription.type == type_)
    if customer:
        q = q.filter(SavioSubscription.customer_id == customer)
    if status:
        q = q.filter(SavioSubscription.status == status)
    if sum_mrr is not None:
        q = q.filter(SavioSubscription.sum_mrr.is_(sum_mrr))

    subs = q.order_by(SavioSubscription.start_date.desc().nullslast()).all()
    return jsonify([s.to_dict() for s in subs])


# ── Invoices / Payments ────────────────────────────────────────────


@savio_bp.route("/invoices", methods=["GET"])
def list_invoices():
    q = SavioInvoice.query
    for arg, col in [("unit", SavioInvoice.unit), ("customer", SavioInvoice.customer_id),
                     ("status", SavioInvoice.status), ("type", SavioInvoice.type)]:
        v = request.args.get(arg)
        if v:
            q = q.filter(col == v)
    df, dt = _parse_date(request.args.get("from")), _parse_date(request.args.get("to"))
    if df:
        q = q.filter(SavioInvoice.date >= df)
    if dt:
        q = q.filter(SavioInvoice.date <= dt)
    return jsonify([i.to_dict() for i in q.order_by(SavioInvoice.date.desc().nullslast()).all()])


@savio_bp.route("/payments", methods=["GET"])
def list_payments():
    q = SavioPayment.query
    customer = request.args.get("customer")
    invoice = request.args.get("invoice")
    df, dt = _parse_date(request.args.get("from")), _parse_date(request.args.get("to"))
    if customer:
        q = q.filter(SavioPayment.customer_id == customer)
    if invoice:
        q = q.filter(SavioPayment.invoice_id == invoice)
    if df:
        q = q.filter(SavioPayment.date >= df)
    if dt:
        q = q.filter(SavioPayment.date <= dt)
    return jsonify([p.to_dict() for p in q.order_by(SavioPayment.date.desc().nullslast()).all()])


# ── MRR report (port de mrrReport de vendedores.cloud) ─────────────


@savio_bp.route("/mrr", methods=["GET"])
def get_mrr():
    """Reporte completo. ?month=YYYY-MM (default: últimos 30 días)."""
    try:
        return jsonify(mrr_report(request.args.get("month")))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@savio_bp.route("/clients", methods=["GET"])
def active_clients():
    """Cuenta de clientes activos (con subs sum_mrr=True). ?unit= opcional."""
    today = datetime.now(timezone.utc).date()
    unit = request.args.get("unit")
    q = (
        db.session.query(SavioSubscription.unit, func.count(func.distinct(SavioSubscription.customer_id)))
        .filter(SavioSubscription.sum_mrr.is_(True))
        .filter(SavioSubscription.start_date <= today)
        .filter(or_(SavioSubscription.contract_end_date.is_(None), SavioSubscription.contract_end_date > today))
    )
    if unit:
        count = q.filter(SavioSubscription.unit == unit).scalar() or 0
        return jsonify({"unit": unit, "count": count})
    rows = q.group_by(SavioSubscription.unit).all()
    return jsonify({"by_unit": {u or "sin_clasificar": int(c) for u, c in rows}})


# ── Sync trigger manual ────────────────────────────────────────────


@savio_bp.route("/sync", methods=["POST"])
def trigger_sync():
    """Dispara sync_all() on-demand. ?month=YYYY-MM filtra invoices+payments."""
    month = request.args.get("month")
    try:
        return jsonify(savio_sync.sync_all(month))
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 500


# ── Customer Master CRUD ───────────────────────────────────────────


@savio_bp.route("/customers/master", methods=["GET"])
def list_masters():
    masters = CustomerMaster.query.order_by(CustomerMaster.master_name.asc().nullslast()).all()
    return jsonify([m.to_dict() for m in masters])


@savio_bp.route("/customers/master/<int:master_id>", methods=["PUT"])
def update_master(master_id):
    master = db.session.get(CustomerMaster, master_id)
    if not master:
        return jsonify({"error": "Master no encontrado"}), 404
    data = request.get_json() or {}
    if "master_name" in data:
        master.master_name = data["master_name"]
    if "zoho_account_id" in data:
        master.zoho_account_id = data["zoho_account_id"]
    if "cs_account_id" in data:
        master.cs_account_id = data["cs_account_id"]
    db.session.commit()
    return jsonify(master.to_dict())


@savio_bp.route("/customers/master/<int:master_id>/merge", methods=["PUT"])
def merge_masters(master_id):
    """Merges merge_id INTO master_id (master_id wins). Mueve todos los RFCs."""
    data = request.get_json() or {}
    merge_id = data.get("merge_id")
    if not merge_id:
        return jsonify({"error": "merge_id requerido"}), 400
    if int(merge_id) == master_id:
        return jsonify({"error": "No se puede mergear consigo mismo"}), 400
    survivor = db.session.get(CustomerMaster, master_id)
    victim = db.session.get(CustomerMaster, int(merge_id))
    if not survivor or not victim:
        return jsonify({"error": "Uno o ambos masters no existen"}), 404
    CustomerRfc.query.filter_by(master_id=victim.id).update({"master_id": survivor.id})
    db.session.delete(victim)
    db.session.commit()
    return jsonify(survivor.to_dict())


# ── Vinculación CS ↔ Savio + sync de facturación ───────────────────


@savio_bp.route("/cs-accounts", methods=["GET"])
def list_cs_accounts_with_links():
    """Devuelve las cuentas CS con su vínculo Savio actual (master + RFCs +
    rollups). Para la UI de vinculación."""
    from models import CSAccount as _CSA
    accounts = _CSA.query.order_by(_CSA.nombre.asc()).all()
    out = []
    for acc in accounts:
        master = CustomerMaster.query.filter_by(cs_account_id=acc.id).first()
        rfcs = master.rfcs if master else []
        out.append({
            "id": str(acc.id), "client_id": acc.client_id or "",
            "nombre": acc.nombre,
            "facturacion_q1": float(acc.facturacion_q1 or 0),
            "pagado_q1": float(acc.pagado_q1 or 0),
            "pendiente_q1": float(acc.pendiente_q1 or 0),
            "num_facturas_q1": int(acc.num_facturas_q1 or 0),
            "master_id": master.id if master else None,
            "rfcs": [r.to_dict() for r in rfcs],
        })
    return jsonify(out)


@savio_bp.route("/customers/search", methods=["GET"])
def search_customers():
    """GET /api/savio/customers/search?q=texto — devuelve hasta 30 SavioCustomers
    matching name, legal_name o tax_id (case-insensitive). Para el modal de
    vinculación de RFCs."""
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify([])
    like = f"%{q}%"
    rows = (
        SavioCustomer.query
        .filter(or_(
            SavioCustomer.name.ilike(like),
            SavioCustomer.legal_name.ilike(like),
            SavioCustomer.tax_id.ilike(like),
        ))
        .order_by(SavioCustomer.name.asc()).limit(30).all()
    )
    return jsonify([{
        "customer_id": c.customer_id,
        "name": c.name, "legal_name": c.legal_name,
        "tax_id": c.tax_id, "city": c.city,
        "current_state": c.current_state,
    } for c in rows])


@savio_bp.route("/customers/master/link", methods=["POST"])
def link_master_to_cs():
    """POST body {cs_account_id, savio_customer_ids: []}
    Crea/actualiza un CustomerMaster vinculado al CSAccount, y sincroniza
    los CustomerRfc para que coincidan exactamente con la lista enviada
    (agrega los nuevos, conserva existentes, no borra los que estén)."""
    from models import CSAccount as _CSA
    data = request.get_json() or {}
    cs_account_id = data.get("cs_account_id")
    savio_ids = data.get("savio_customer_ids") or []
    if not cs_account_id:
        return jsonify({"error": "cs_account_id requerido"}), 400

    acc = db.session.get(_CSA, cs_account_id)
    if not acc:
        return jsonify({"error": "CSAccount no existe"}), 404

    # Buscar master existente vinculado a esa CSAccount
    master = CustomerMaster.query.filter_by(cs_account_id=acc.id).first()
    if not master:
        master = CustomerMaster(master_name=acc.nombre, cs_account_id=acc.id)
        db.session.add(master)
        db.session.flush()

    # Para cada savio_customer_id, asegurar un CustomerRfc bajo este master
    added = 0
    skipped = 0
    not_found = []
    for cid in savio_ids:
        sc = db.session.get(SavioCustomer, str(cid))
        if not sc or not sc.tax_id:
            not_found.append(cid)
            continue
        existing = CustomerRfc.query.filter_by(rfc=sc.tax_id).first()
        if existing:
            # Si está bajo otro master, lo movemos a este
            if existing.master_id != master.id:
                existing.master_id = master.id
                existing.savio_customer_id = sc.customer_id
                added += 1
            else:
                skipped += 1
        else:
            db.session.add(CustomerRfc(
                master_id=master.id, rfc=sc.tax_id,
                legal_name=sc.legal_name or sc.name or "",
                savio_customer_id=sc.customer_id,
            ))
            added += 1
    db.session.commit()
    return jsonify({
        "master_id": master.id,
        "cs_account_id": str(acc.id),
        "added_or_moved": added, "already_linked": skipped,
        "not_found": not_found,
        "current_rfcs": [r.to_dict() for r in master.rfcs],
    })


@savio_bp.route("/cs-sync", methods=["POST"])
def trigger_cs_sync():
    """POST {account_id} (opcional). Sin account_id sincroniza las 25 cuentas.
    Pulla SavioInvoices vía CustomerMaster→CustomerRfc y upserta CSInvoice."""
    data = request.get_json(silent=True) or {}
    account_id = data.get("account_id")
    try:
        return jsonify(savio_sync.sync_savio_to_cs_invoices(account_id=account_id))
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 500


# ── Webhook receiver ───────────────────────────────────────────────


def _verify_webhook(timestamp: str, raw_body: bytes, signature: str) -> tuple[bool, str]:
    secret = os.getenv("SAVIO_WEBHOOK_SECRET", "")
    if not secret:
        return False, "SAVIO_WEBHOOK_SECRET no configurada"
    if not timestamp or not signature:
        return False, "headers faltantes"
    payload = f"{timestamp}.".encode() + raw_body
    expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return False, "firma inválida"
    return True, "ok"


@savio_bp.route("/webhook", methods=["POST"])
def webhook_receiver():
    """Recibe eventos de Savio. Verifica HMAC y dispara sync incremental."""
    ts = request.headers.get("X-Webhook-Timestamp", "")
    sig = request.headers.get("X-Webhook-Signature", "")
    ok, reason = _verify_webhook(ts, request.get_data(), sig)
    if not ok:
        return jsonify({"error": reason}), 401
    body = request.get_json(silent=True) or {}
    evt = body.get("event_type", "")
    try:
        if evt in ("payment.created", "payment.deleted"):
            savio_sync.sync_payments()
        elif evt in ("invoice.status_updated", "invoice.deleted"):
            savio_sync.sync_invoices()
    except Exception as e:
        return jsonify({"ok": True, "warning": f"sync falló: {e}"}), 200
    return jsonify({"ok": True, "event": evt})

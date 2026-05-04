# blueprints/savio.py
"""
Savio — endpoints de lectura sobre los datos sincronizados.
Rutas bajo /api/savio/. La conexión real con la API de Savio (sync) se
agrega en un paso posterior; mientras tanto estos endpoints leen de las
tablas savio_* y devuelven listas vacías hasta que el sync corra.
"""
from datetime import datetime
from flask import Blueprint, request, jsonify
from sqlalchemy import func, or_

from extensions import db
from models import SavioCustomer, SavioSubscription, SavioInvoice, SavioPayment

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


@savio_bp.route("/customers", methods=["GET"])
def list_customers():
    """Filtros: ?unit= ?state= ?search= (matchea name o legal_name)."""
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

    customers = q.order_by(SavioCustomer.name.asc()).all()
    return jsonify([c.to_dict() for c in customers])


@savio_bp.route("/customers/<string:customer_id>", methods=["GET"])
def get_customer(customer_id):
    customer = db.session.get(SavioCustomer, customer_id)
    if not customer:
        return jsonify({"error": "Customer no encontrado"}), 404
    payload = customer.to_dict()
    payload["subscriptions"] = [s.to_dict() for s in customer.subscriptions]
    return jsonify(payload)


@savio_bp.route("/subscriptions", methods=["GET"])
def list_subscriptions():
    """Filtros: ?unit= ?type= ?customer= ?status= ?sum_mrr=true|false."""
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


@savio_bp.route("/invoices", methods=["GET"])
def list_invoices():
    """Filtros: ?unit= ?customer= ?status= ?from=YYYY-MM-DD ?to=YYYY-MM-DD."""
    q = SavioInvoice.query
    unit = request.args.get("unit")
    customer = request.args.get("customer")
    status = request.args.get("status")
    date_from = _parse_date(request.args.get("from"))
    date_to = _parse_date(request.args.get("to"))

    if unit:
        q = q.filter(SavioInvoice.unit == unit)
    if customer:
        q = q.filter(SavioInvoice.customer_id == customer)
    if status:
        q = q.filter(SavioInvoice.status == status)
    if date_from:
        q = q.filter(SavioInvoice.date >= date_from)
    if date_to:
        q = q.filter(SavioInvoice.date <= date_to)

    invoices = q.order_by(SavioInvoice.date.desc().nullslast()).all()
    return jsonify([i.to_dict() for i in invoices])


@savio_bp.route("/payments", methods=["GET"])
def list_payments():
    """Filtros: ?customer= ?invoice= ?from=YYYY-MM-DD ?to=YYYY-MM-DD."""
    q = SavioPayment.query
    customer = request.args.get("customer")
    invoice = request.args.get("invoice")
    date_from = _parse_date(request.args.get("from"))
    date_to = _parse_date(request.args.get("to"))

    if customer:
        q = q.filter(SavioPayment.customer_id == customer)
    if invoice:
        q = q.filter(SavioPayment.invoice_id == invoice)
    if date_from:
        q = q.filter(SavioPayment.date >= date_from)
    if date_to:
        q = q.filter(SavioPayment.date <= date_to)

    payments = q.order_by(SavioPayment.date.desc().nullslast()).all()
    return jsonify([p.to_dict() for p in payments])


@savio_bp.route("/mrr", methods=["GET"])
def mrr_summary():
    """MRR pre-IVA agregado por unidad. Solo cuenta subscriptions con
    sum_mrr=True. Devuelve {by_unit, total, subscription_count}."""
    rows = (
        db.session.query(
            SavioSubscription.unit,
            func.coalesce(func.sum(SavioSubscription.mrr), 0).label("total"),
            func.count(SavioSubscription.id).label("count"),
        )
        .filter(SavioSubscription.sum_mrr.is_(True))
        .group_by(SavioSubscription.unit)
        .all()
    )

    by_unit = {}
    total = 0.0
    sub_count = 0
    for unit, unit_total, count in rows:
        amount = float(unit_total or 0)
        by_unit[unit or "sin_unit"] = {
            "mrr": amount,
            "subscription_count": int(count),
        }
        total += amount
        sub_count += int(count)

    return jsonify({
        "by_unit": by_unit,
        "total": total,
        "subscription_count": sub_count,
    })

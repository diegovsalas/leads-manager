# blueprints/sales.py
"""
Sales + comisiones + clientes post-venta.

Port de los endpoints /api/sales/* y /api/clients/* de vendedores.cloud
(server.js ~360-499). Reglas de comisión:
  - autogenerado:  rate 1.0
  - lead_otorgado: rate 0.5
  - subscription/upsell: commission = monthly_amount * rate
  - servicio_unico:      commission = total_amount * 0.08
"""
from datetime import date, datetime, timezone
from decimal import Decimal
from flask import Blueprint, request, jsonify, session
from sqlalchemy import or_

from extensions import db
from models import Sale, Client, Lead, EtapaPipeline, Usuario

sales_bp = Blueprint("sales", __name__)
clients_bp = Blueprint("clients", __name__)


# ── Comisiones ─────────────────────────────────────────────────────


def _calc_commission(sale_type: str, commission_type: str | None,
                     monthly_amount: float, total_amount: float) -> tuple[float, float]:
    """Devuelve (rate, amount). Replica la fórmula del legacy 1:1."""
    rate = 1.0 if commission_type == "autogenerado" else 0.5
    if sale_type in ("suscripcion_nueva", "upsell"):
        amount = (monthly_amount or 0) * rate
    else:  # servicio_unico u otros
        amount = (total_amount or 0) * 0.08
    return rate, amount


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


def _current_user_id():
    """Devuelve el UUID del Usuario (vendedor) en sesión, o None.
    Sale.user_id es FK a usuarios.id — NO usar session['user_id'] (eso es users.id)."""
    return session.get("usuario_id")


# ── Sales ──────────────────────────────────────────────────────────


@sales_bp.route("/", methods=["GET"])
def list_sales():
    """Filtros: ?unit=&user_id=&commission_status=&status="""
    q = Sale.query
    unit = request.args.get("unit")
    user_id = request.args.get("user_id")
    cs = request.args.get("commission_status")
    st = request.args.get("status")
    if unit:
        q = q.filter(Sale.unit == unit)
    if user_id:
        q = q.filter(Sale.user_id == user_id)
    if cs:
        q = q.filter(Sale.commission_status == cs)
    if st:
        q = q.filter(Sale.status == st)
    sales = q.order_by(Sale.closed_at.desc().nullslast()).all()
    return jsonify([s.to_dict() for s in sales])


@sales_bp.route("/<uuid:sale_id>", methods=["GET"])
def get_sale(sale_id):
    sale = db.session.get(Sale, sale_id)
    if not sale:
        return jsonify({"error": "Venta no encontrada"}), 404
    return jsonify(sale.to_dict())


@sales_bp.route("/", methods=["POST"])
def create_sale():
    """Crea una venta calculando la comisión y marcando el lead como ganado."""
    data = request.get_json() or {}
    unit = data.get("unit")
    sale_type = data.get("sale_type")
    if not unit or not sale_type:
        return jsonify({"error": "unit y sale_type requeridos"}), 400

    monthly = float(data.get("monthly_amount") or 0)
    total = float(data.get("total_amount") or 0)
    commission_type = data.get("commission_type")
    rate, amount = _calc_commission(sale_type, commission_type, monthly, total)

    lead_id = data.get("lead_id")
    opportunity_id = data.get("opportunity_id")
    if opportunity_id:
        existing = Sale.query.filter(Sale.opportunity_id == opportunity_id).first()
        if existing:
            return jsonify(existing.to_dict()), 200

    lead_source = None
    if lead_id:
        lead = db.session.get(Lead, lead_id)
        if lead:
            lead_source = lead.origen.value if lead.origen else None

    sale = Sale(
        lead_id=lead_id, opportunity_id=opportunity_id, user_id=_current_user_id(),
        unit=unit, sale_type=sale_type,
        sale_category=data.get("sale_category") or "recurrente",
        uen=data.get("uen"),
        lead_source=lead_source,
        monthly_amount=Decimal(str(monthly)),
        total_amount=Decimal(str(total)),
        commission_type=commission_type,
        commission_rate=Decimal(str(rate)),
        commission_amount=Decimal(str(amount)),
        closed_at=_parse_dt(data.get("closed_at")) or datetime.now(timezone.utc),
        contract_signed_at=_parse_dt(data.get("contract_signed_at")),
        first_payment_at=_parse_dt(data.get("first_payment_at")),
        service_start_at=_parse_dt(data.get("service_start_at")),
    )
    db.session.add(sale)

    # Marcar lead como ganado
    if lead_id:
        lead = db.session.get(Lead, lead_id)
        if lead:
            lead.etapa_pipeline = EtapaPipeline.CIERRE_GANADO

    db.session.commit()
    return jsonify(sale.to_dict()), 201


@sales_bp.route("/<uuid:sale_id>", methods=["PATCH"])
def update_sale(sale_id):
    sale = db.session.get(Sale, sale_id)
    if not sale:
        return jsonify({"error": "Venta no encontrada"}), 404
    data = request.get_json() or {}

    # Recalcular comisión si cambian los amounts/types
    recompute = any(k in data for k in ("sale_type", "commission_type", "monthly_amount", "total_amount"))
    if "sale_type" in data:
        sale.sale_type = data["sale_type"]
    if "commission_type" in data:
        sale.commission_type = data["commission_type"]
    if "monthly_amount" in data:
        sale.monthly_amount = Decimal(str(data["monthly_amount"] or 0))
    if "total_amount" in data:
        sale.total_amount = Decimal(str(data["total_amount"] or 0))
    if recompute:
        rate, amount = _calc_commission(
            sale.sale_type, sale.commission_type,
            float(sale.monthly_amount or 0), float(sale.total_amount or 0),
        )
        sale.commission_rate = Decimal(str(rate))
        sale.commission_amount = Decimal(str(amount))

    for fld in ("sale_category", "uen", "status", "cancel_reason",
                "commission_status"):
        if fld in data:
            setattr(sale, fld, data[fld])
    for fld_dt in ("contract_signed_at", "first_payment_at", "service_start_at",
                   "commission_pay_date", "canceled_at"):
        if fld_dt in data:
            setattr(sale, fld_dt, _parse_dt(data[fld_dt]))

    if data.get("status") == "cancelada" and not sale.canceled_at:
        sale.canceled_at = datetime.now(timezone.utc)

    db.session.commit()
    return jsonify(sale.to_dict())


@sales_bp.route("/<uuid:sale_id>", methods=["DELETE"])
def delete_sale(sale_id):
    sale = db.session.get(Sale, sale_id)
    if not sale:
        return jsonify({"error": "Venta no encontrada"}), 404
    db.session.delete(sale)
    db.session.commit()
    return jsonify({"ok": True})


@sales_bp.route("/stats", methods=["GET"])
def sales_stats():
    """Resumen de comisiones por status + total por unidad."""
    base = Sale.query
    user_id = request.args.get("user_id")
    unit = request.args.get("unit")
    if user_id:
        base = base.filter(Sale.user_id == user_id)
    if unit:
        base = base.filter(Sale.unit == unit)

    by_status_rows = (
        base.with_entities(Sale.commission_status, db.func.count(),
                           db.func.coalesce(db.func.sum(Sale.commission_amount), 0))
        .group_by(Sale.commission_status).all()
    )
    by_unit_rows = (
        base.with_entities(Sale.unit, db.func.count(),
                           db.func.coalesce(db.func.sum(Sale.commission_amount), 0),
                           db.func.coalesce(db.func.sum(Sale.monthly_amount), 0))
        .group_by(Sale.unit).all()
    )
    return jsonify({
        "by_commission_status": [
            {"status": s, "count": c, "amount": float(a or 0)}
            for s, c, a in by_status_rows
        ],
        "by_unit": [
            {"unit": u, "count": c, "commission_total": float(ct or 0),
             "monthly_total": float(mt or 0)}
            for u, c, ct, mt in by_unit_rows
        ],
    })


# ── Clientes post-venta ────────────────────────────────────────────


@clients_bp.route("/", methods=["GET"])
def list_clients():
    q = Client.query
    unit = request.args.get("unit")
    status = request.args.get("status")
    search = request.args.get("search", "").strip()
    if unit:
        q = q.filter(Client.unit == unit)
    if status:
        q = q.filter(Client.status == status)
    if search:
        like = f"%{search}%"
        q = q.filter(or_(
            Client.company.ilike(like),
            Client.trade_name.ilike(like),
            Client.rfc.ilike(like),
        ))
    rows = q.order_by(Client.created_at.desc()).all()
    return jsonify([r.to_dict() for r in rows])


@clients_bp.route("/<uuid:client_id>", methods=["GET"])
def get_client(client_id):
    c = db.session.get(Client, client_id)
    if not c:
        return jsonify({"error": "Cliente no encontrado"}), 404
    return jsonify(c.to_dict())


@clients_bp.route("/", methods=["POST"])
def create_client():
    data = request.get_json() or {}
    if not data.get("company") or not data.get("unit"):
        return jsonify({"error": "company y unit requeridos"}), 400
    c = Client(
        sale_id=data.get("sale_id"),
        company=data["company"], trade_name=data.get("trade_name"),
        rfc=data.get("rfc"), service_address=data.get("service_address"),
        city=data.get("city"), unit=data["unit"],
        service=data.get("service"), frequency=data.get("frequency"),
        monthly_amount=Decimal(str(data["monthly_amount"])) if data.get("monthly_amount") else None,
        contract_start=_parse_date(data.get("contract_start")),
        contract_end=_parse_date(data.get("contract_end")),
        assigned_to=data.get("assigned_to"),
    )
    db.session.add(c)
    db.session.commit()
    return jsonify(c.to_dict()), 201


@clients_bp.route("/<uuid:client_id>", methods=["PATCH"])
def update_client(client_id):
    c = db.session.get(Client, client_id)
    if not c:
        return jsonify({"error": "Cliente no encontrado"}), 404
    data = request.get_json() or {}
    for fld in ("company", "trade_name", "rfc", "service_address", "city",
                "unit", "service", "frequency", "status", "cancel_reason",
                "assigned_to", "nps_score"):
        if fld in data:
            setattr(c, fld, data[fld])
    if "monthly_amount" in data:
        c.monthly_amount = Decimal(str(data["monthly_amount"])) if data["monthly_amount"] else None
    if "contract_start" in data:
        c.contract_start = _parse_date(data["contract_start"])
    if "contract_end" in data:
        c.contract_end = _parse_date(data["contract_end"])
    if "nps_date" in data or "nps_score" in data:
        c.nps_date = datetime.now(timezone.utc) if data.get("nps_score") else c.nps_date
    db.session.commit()
    return jsonify(c.to_dict())


@clients_bp.route("/<uuid:client_id>", methods=["DELETE"])
def delete_client(client_id):
    c = db.session.get(Client, client_id)
    if not c:
        return jsonify({"error": "Cliente no encontrado"}), 404
    db.session.delete(c)
    db.session.commit()
    return jsonify({"ok": True})

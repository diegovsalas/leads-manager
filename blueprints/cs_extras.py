# blueprints/cs_extras.py
"""
Endpoints de soporte CS no cubiertos en blueprints/cs.py:
- /api/touchpoints: post-sale touchpoints por cliente.
- /api/weekly-kpis: KPIs semanales por vendedor.
- /api/city-assignments y /api/state-assignments: routing geográfico.

Todos siguen el patrón del legacy server.js (~493-499) pero con los
modelos UUID-based de leads-manager.
"""
from datetime import datetime, timezone, date
from flask import Blueprint, request, jsonify
from sqlalchemy import func

from extensions import db
from models import Touchpoint, WeeklyKpi, CityAssignment, StateAssignment

touchpoints_bp = Blueprint("touchpoints", __name__)
weekly_kpis_bp = Blueprint("weekly_kpis", __name__)
assignments_bp = Blueprint("assignments", __name__)


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


# ── Touchpoints ────────────────────────────────────────────────────


@touchpoints_bp.route("/", methods=["GET"])
def list_touchpoints():
    q = Touchpoint.query
    cid = request.args.get("client_id")
    uid = request.args.get("user_id")
    status = request.args.get("status")
    if cid:
        q = q.filter(Touchpoint.client_id == cid)
    if uid:
        q = q.filter(Touchpoint.user_id == uid)
    if status:
        q = q.filter(Touchpoint.status == status)
    rows = q.order_by(Touchpoint.scheduled_at.asc().nullslast()).all()
    return jsonify([r.to_dict() for r in rows])


@touchpoints_bp.route("/", methods=["POST"])
def create_touchpoint():
    d = request.get_json() or {}
    if not d.get("day_number") or not d.get("type"):
        return jsonify({"error": "day_number y type requeridos"}), 400
    tp = Touchpoint(
        client_id=d.get("client_id"), user_id=d.get("user_id"),
        day_number=int(d["day_number"]), type=d["type"],
        status=d.get("status") or "pendiente",
        scheduled_at=_parse_dt(d.get("scheduled_at")),
        completed_at=_parse_dt(d.get("completed_at")),
        notes=d.get("notes"),
    )
    db.session.add(tp)
    db.session.commit()
    return jsonify(tp.to_dict()), 201


@touchpoints_bp.route("/<int:tp_id>", methods=["PATCH"])
def update_touchpoint(tp_id):
    tp = db.session.get(Touchpoint, tp_id)
    if not tp:
        return jsonify({"error": "Touchpoint no encontrado"}), 404
    d = request.get_json() or {}
    for fld in ("day_number", "type", "status", "notes", "user_id", "client_id"):
        if fld in d:
            setattr(tp, fld, d[fld])
    if "scheduled_at" in d:
        tp.scheduled_at = _parse_dt(d["scheduled_at"])
    if "completed_at" in d:
        tp.completed_at = _parse_dt(d["completed_at"])
    if d.get("status") == "completado" and not tp.completed_at:
        tp.completed_at = datetime.now(timezone.utc)
    db.session.commit()
    return jsonify(tp.to_dict())


@touchpoints_bp.route("/<int:tp_id>", methods=["DELETE"])
def delete_touchpoint(tp_id):
    tp = db.session.get(Touchpoint, tp_id)
    if not tp:
        return jsonify({"error": "Touchpoint no encontrado"}), 404
    db.session.delete(tp)
    db.session.commit()
    return jsonify({"ok": True})


# ── Weekly KPIs ────────────────────────────────────────────────────


@weekly_kpis_bp.route("/", methods=["GET"])
def list_kpis():
    q = WeeklyKpi.query
    uid = request.args.get("user_id")
    week = _parse_date(request.args.get("week_start"))
    if uid:
        q = q.filter(WeeklyKpi.user_id == uid)
    if week:
        q = q.filter(WeeklyKpi.week_start == week)
    rows = q.order_by(WeeklyKpi.week_start.desc()).all()
    return jsonify([r.to_dict() for r in rows])


@weekly_kpis_bp.route("/", methods=["POST"])
def create_or_update_kpi():
    """Upsert por (user_id, week_start). Si existe, actualiza."""
    d = request.get_json() or {}
    user_id = d.get("user_id")
    week_start = _parse_date(d.get("week_start"))
    if not user_id or not week_start:
        return jsonify({"error": "user_id y week_start requeridos"}), 400

    existing = WeeklyKpi.query.filter_by(user_id=user_id, week_start=week_start).first()
    row = existing or WeeklyKpi(user_id=user_id, week_start=week_start)
    for fld in ("calls_made", "whatsapps_sent", "emails_sent", "quotes_sent",
                "visits_made", "leads_generated", "crm_compliance",
                "target_calls", "target_whatsapps", "target_emails",
                "target_quotes", "target_visits"):
        if fld in d:
            setattr(row, fld, d[fld])

    # Auto-compliance: avg de % alcanzado vs target
    pcts = []
    for actual_fld, tgt_fld in [
        ("calls_made", "target_calls"), ("whatsapps_sent", "target_whatsapps"),
        ("emails_sent", "target_emails"), ("quotes_sent", "target_quotes"),
        ("visits_made", "target_visits"),
    ]:
        actual = getattr(row, actual_fld) or 0
        tgt = getattr(row, tgt_fld) or 0
        if tgt > 0:
            pcts.append(min(100.0, (actual / tgt) * 100))
    row.compliance_pct = round(sum(pcts) / len(pcts), 1) if pcts else 0
    if not existing:
        db.session.add(row)
    db.session.commit()
    return jsonify(row.to_dict())


@weekly_kpis_bp.route("/<int:kpi_id>", methods=["DELETE"])
def delete_kpi(kpi_id):
    row = db.session.get(WeeklyKpi, kpi_id)
    if not row:
        return jsonify({"error": "KPI no encontrado"}), 404
    db.session.delete(row)
    db.session.commit()
    return jsonify({"ok": True})


# ── City + State assignments ───────────────────────────────────────


@assignments_bp.route("/cities", methods=["GET"])
def list_cities():
    q = CityAssignment.query
    unit = request.args.get("unit")
    if unit:
        q = q.filter(CityAssignment.unit == unit)
    rows = q.order_by(CityAssignment.city.asc(),
                      CityAssignment.round_robin_order.asc()).all()
    return jsonify([r.to_dict() for r in rows])


@assignments_bp.route("/cities", methods=["POST"])
def create_city_assignment():
    d = request.get_json() or {}
    if not d.get("city") or not d.get("unit"):
        return jsonify({"error": "city y unit requeridos"}), 400
    row = CityAssignment(
        city=d["city"], unit=d["unit"], user_id=d.get("user_id"),
        round_robin_order=int(d.get("round_robin_order") or 0),
        active=bool(d.get("active", True)),
    )
    db.session.add(row)
    db.session.commit()
    return jsonify(row.to_dict()), 201


@assignments_bp.route("/cities/<int:aid>", methods=["PATCH"])
def patch_city_assignment(aid):
    row = db.session.get(CityAssignment, aid)
    if not row:
        return jsonify({"error": "Assignment no encontrado"}), 404
    d = request.get_json() or {}
    for fld in ("city", "unit", "user_id", "round_robin_order", "active"):
        if fld in d:
            setattr(row, fld, d[fld])
    db.session.commit()
    return jsonify(row.to_dict())


@assignments_bp.route("/cities/<int:aid>", methods=["DELETE"])
def delete_city_assignment(aid):
    row = db.session.get(CityAssignment, aid)
    if not row:
        return jsonify({"error": "Assignment no encontrado"}), 404
    db.session.delete(row)
    db.session.commit()
    return jsonify({"ok": True})


@assignments_bp.route("/states", methods=["GET"])
def list_states():
    q = StateAssignment.query
    unit = request.args.get("unit")
    if unit:
        q = q.filter(StateAssignment.unit == unit)
    rows = q.order_by(StateAssignment.state.asc(), StateAssignment.unit.asc()).all()
    return jsonify([r.to_dict() for r in rows])


@assignments_bp.route("/states", methods=["POST"])
def create_state_assignment():
    d = request.get_json() or {}
    if not d.get("state") or not d.get("unit"):
        return jsonify({"error": "state y unit requeridos"}), 400
    row = StateAssignment(
        state=d["state"], unit=d["unit"], user_id=d.get("user_id"),
    )
    try:
        db.session.add(row)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 400
    return jsonify(row.to_dict()), 201


@assignments_bp.route("/states/<int:aid>", methods=["DELETE"])
def delete_state_assignment(aid):
    row = db.session.get(StateAssignment, aid)
    if not row:
        return jsonify({"error": "Assignment no encontrado"}), 404
    db.session.delete(row)
    db.session.commit()
    return jsonify({"ok": True})


@assignments_bp.route("/states/lookup", methods=["GET"])
def lookup_state_assignment():
    """GET /api/assignments/states/lookup?state=&unit= → user_id assignado o null.
    Usado por el SDR Prospector para auto-asignar leads por estado."""
    state = request.args.get("state")
    unit = request.args.get("unit")
    if not state or not unit:
        return jsonify({"error": "state y unit requeridos"}), 400
    row = StateAssignment.query.filter_by(state=state, unit=unit).first()
    return jsonify({"user_id": str(row.user_id) if row and row.user_id else None})

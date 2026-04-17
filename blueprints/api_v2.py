# blueprints/api_v2.py
"""
API v2 — JSON endpoints for React frontend.
"""
from datetime import date
from flask import Blueprint, jsonify, request, session
from sqlalchemy import func
from extensions import db
from models import (
    CSAccount, CSInvoice, CSAppointment, CSNote, CSTask,
    CSOnboardingAccount, CSOpportunity, CSContacto, UserCRM, RolCRM,
)
from cs_health_score import calcular_health_scores_batch
from cs_alerts import generar_alertas

api_v2_bp = Blueprint("api_v2", __name__)


def _get_periodo():
    param = request.args.get("periodo", "")
    if param and "-Q" in param:
        year = int(param.split("-Q")[0])
        quarter = int(param.split("-Q")[1])
        month_start = (quarter - 1) * 3 + 1
        inicio = date(year, month_start, 1)
        fin = date(year + 1, 1, 1) if month_start + 3 > 12 else date(year, month_start + 3, 1)
        label = f"Q{quarter} {year}"
    elif param and len(param) == 7 and "-Q" not in param:
        year, month = int(param[:4]), int(param[5:7])
        inicio = date(year, month, 1)
        fin = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
        meses = ["", "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
                 "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"]
        label = f"{meses[month]} {year}"
    elif param == "all":
        inicio, fin, label = date(2020, 1, 1), date(2030, 1, 1), "Todo el historial"
    else:
        hoy = date.today()
        inicio = hoy.replace(day=1)
        fin = date(hoy.year + 1, 1, 1) if hoy.month == 12 else date(hoy.year, hoy.month + 1, 1)
        meses = ["", "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
                 "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"]
        label = f"{meses[hoy.month]} {hoy.year}"
    return inicio, fin, label


def _calc_facturacion(account_ids, inicio, fin):
    rows = db.session.query(
        CSInvoice.account_id,
        func.coalesce(func.sum(CSInvoice.total), 0),
        func.coalesce(func.sum(CSInvoice.pagado), 0),
        func.coalesce(func.sum(CSInvoice.pendiente), 0),
    ).filter(
        CSInvoice.account_id.in_(account_ids),
        CSInvoice.fecha_cobro >= inicio, CSInvoice.fecha_cobro < fin,
    ).group_by(CSInvoice.account_id).all()
    return {str(r[0]): {"facturado": float(r[1]), "pagado": float(r[2]), "pendiente": float(r[3])} for r in rows}


@api_v2_bp.route("/cs/dashboard")
def cs_dashboard():
    inicio, fin, label = _get_periodo()
    accounts = CSAccount.query.all()
    account_ids = [a.id for a in accounts]
    scores_map = calcular_health_scores_batch(accounts)
    fact = _calc_facturacion(account_ids, inicio, fin)

    cat_counts = {"Sana": 0, "Atención": 0, "Riesgo": 0}
    top_riesgo = []
    for acc in accounts:
        hs = scores_map[str(acc.id)]
        cat_counts[hs["categoria"]] += 1
        top_riesgo.append({
            "id": str(acc.id), "nombre": acc.nombre,
            "mrr": float(acc.mrr or 0), "sucursales": acc.sucursales,
            "unidades_contratadas": acc.unidades_contratadas,
            "tier": acc.tier, "giro": acc.giro,
            "score": hs["score"], "categoria": hs["categoria"], "color": hs["color"],
            "kam_nombre": acc.kam.nombre if acc.kam else "",
        })
    top_riesgo.sort(key=lambda x: x["score"])

    kams = UserCRM.query.filter_by(rol=RolCRM.KAM, activo=True).order_by(UserCRM.nombre).all()
    kam_data = []
    for k in kams:
        accs = [a for a in accounts if str(a.kam_id) == str(k.id)]
        kam_data.append({
            "id": str(k.id), "nombre": k.nombre,
            "num_cuentas": len(accs),
            "mrr": sum(float(a.mrr or 0) for a in accs),
            "sucursales": sum(a.sucursales for a in accs),
        })

    alertas = generar_alertas(accounts=accounts, scores_map=scores_map)

    return jsonify({
        "mrr_total": sum(float(a.mrr or 0) for a in accounts),
        "arr_total": sum(float(a.arr_proyectado or 0) for a in accounts),
        "num_cuentas": len(accounts),
        "total_sucursales": sum(a.sucursales for a in accounts),
        "facturado_periodo": sum(f["facturado"] for f in fact.values()),
        "pagado_periodo": sum(f["pagado"] for f in fact.values()),
        "pendiente_periodo": sum(f["pendiente"] for f in fact.values()),
        "cat_counts": cat_counts,
        "top_riesgo": top_riesgo[:5],
        "kam_data": kam_data,
        "alertas": alertas,
        "periodo_label": label,
    })


@api_v2_bp.route("/cs/clientes")
def cs_clientes():
    accounts = CSAccount.query.order_by(CSAccount.nombre).all()
    scores_map = calcular_health_scores_batch(accounts)
    result = []
    for acc in accounts:
        hs = scores_map[str(acc.id)]
        owners = CSContacto.query.filter_by(account_id=acc.id, is_owner=True).all()
        result.append({
            "id": str(acc.id), "nombre": acc.nombre,
            "mrr": float(acc.mrr or 0), "sucursales": acc.sucursales,
            "unidades_contratadas": acc.unidades_contratadas,
            "tier": acc.tier, "giro": acc.giro,
            "score": hs["score"], "categoria": hs["categoria"], "color": hs["color"],
            "nps": acc.nps,
            "kam_nombre": acc.kam.nombre if acc.kam else "",
            "owners": [{"nombre": o.nombre, "correo": o.correo} for o in owners],
        })
    return jsonify(result)

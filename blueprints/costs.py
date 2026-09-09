# blueprints/costs.py
"""
API cost reports. Port de /api/costs/* de vendedores.cloud.
"""
from flask import Blueprint, request, jsonify

import api_costs

costs_bp = Blueprint("costs", __name__)

# FEAT-2026-09-09: guardia real. Gasto de infraestructura, no dato comercial.
# El menu ocultaba el enlace con display:none, pero el endpoint
# respondia 200 a cualquiera con sesion iniciada. Va como
# before_request para que una ruta nueva nazca protegida.
from blueprints.auth import guardia_sistema  # noqa: E402
costs_bp.before_request(guardia_sistema)


@costs_bp.route("/summary", methods=["GET"])
def summary():
    """?period=today|week|month  o  ?from=YYYY-MM-DD&to=YYYY-MM-DD"""
    return jsonify(api_costs.get_cost_summary(
        period=request.args.get("period"),
        date_from=request.args.get("from"),
        date_to=request.args.get("to"),
    ))


@costs_bp.route("/detail", methods=["GET"])
def detail():
    """?service=&unit=&page=&limit="""
    try:
        page = int(request.args.get("page") or 1)
        limit = int(request.args.get("limit") or 50)
    except ValueError:
        page, limit = 1, 50
    return jsonify(api_costs.get_cost_detail(
        service=request.args.get("service"),
        unit=request.args.get("unit"),
        page=page, limit=limit,
    ))

# blueprints/costs.py
"""
API cost reports. Port de /api/costs/* de vendedores.cloud.
"""
from flask import Blueprint, request, jsonify

import api_costs

costs_bp = Blueprint("costs", __name__)


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

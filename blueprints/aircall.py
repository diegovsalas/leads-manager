# blueprints/aircall.py
"""
Aircall stats endpoints. Port de /api/aircall/* de vendedores.cloud.
"""
from flask import Blueprint, request, jsonify

import aircall

aircall_bp = Blueprint("aircall", __name__)


@aircall_bp.route("/status", methods=["GET"])
def status():
    return jsonify(aircall.get_connection_info())


@aircall_bp.route("/users", methods=["GET"])
def users():
    try:
        return jsonify(aircall.get_users())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@aircall_bp.route("/stats", methods=["GET"])
def stats():
    """?from=ISO&to=ISO  (ej. 2026-04-01 / 2026-04-30)"""
    try:
        return jsonify(aircall.get_call_stats(
            from_dt=request.args.get("from"),
            to_dt=request.args.get("to"),
        ))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@aircall_bp.route("/calls", methods=["GET"])
def calls():
    try:
        page = int(request.args.get("page") or 1)
        per_page = min(int(request.args.get("per_page") or 50), 100)
    except ValueError:
        page, per_page = 1, 50
    try:
        return jsonify(aircall.get_calls(
            from_dt=request.args.get("from"),
            to_dt=request.args.get("to"),
            page=page, per_page=per_page,
        ))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

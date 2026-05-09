# blueprints/zoho.py
"""
Zoho CRM endpoints. Port de /api/zoho/* + auth callback.
"""
from datetime import datetime, timezone
from flask import Blueprint, request, jsonify, redirect

from extensions import db
import zoho

zoho_bp = Blueprint("zoho", __name__)


@zoho_bp.route("/status", methods=["GET"])
def status():
    return jsonify(zoho.get_connection_info())


@zoho_bp.route("/connect", methods=["GET"])
def connect():
    """Inicia el flujo OAuth de Zoho. Redirige al usuario."""
    return redirect(zoho.get_auth_url())


# Callback handler — Zoho redirige acá con ?code=xxx
@zoho_bp.route("/auth/zoho/callback", methods=["GET"])
def callback():
    code = request.args.get("code")
    if not code:
        return jsonify({"error": "no code in callback"}), 400
    try:
        result = zoho.exchange_code(code)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    if not result.get("ok"):
        return jsonify(result), 400
    # Redirigir al app principal con flag de éxito
    return redirect("/?zoho=ok")


@zoho_bp.route("/leads", methods=["GET"])
def leads():
    try:
        page = int(request.args.get("page") or 1)
    except ValueError:
        page = 1
    try:
        return jsonify(zoho.get_leads(page=page))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@zoho_bp.route("/deals", methods=["GET"])
def deals():
    try:
        page = int(request.args.get("page") or 1)
    except ValueError:
        page = 1
    try:
        return jsonify(zoho.get_deals(page=page))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@zoho_bp.route("/users", methods=["GET"])
def users():
    try:
        return jsonify(zoho.get_users())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@zoho_bp.route("/contacts", methods=["GET"])
def contacts():
    try:
        page = int(request.args.get("page") or 1)
    except ValueError:
        page = 1
    try:
        return jsonify(zoho.get_contacts(page=page))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@zoho_bp.route("/sync", methods=["POST"])
def sync():
    """Trigger sync incremental — pulls últimos modificados.
    Body opcional: {since: 'YYYY-MM-DD'} (default: hoy 00:00)."""
    body = request.get_json(silent=True) or {}
    since = body.get("since")
    if not since:
        since = datetime.now(timezone.utc).strftime("%a, %d %b %Y 00:00:00 GMT")
    try:
        leads_data = zoho.get_modified_leads(since)
        deals_data = zoho.get_modified_deals(since)
        return jsonify({
            "ok": True,
            "leads_count": len(leads_data.get("data") or []),
            "deals_count": len(deals_data.get("data") or []),
            "since": since,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@zoho_bp.route("/last-sync", methods=["GET"])
def last_sync():
    info = zoho.get_connection_info()
    return jsonify(info)

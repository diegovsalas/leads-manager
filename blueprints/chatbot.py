# blueprints/chatbot.py
"""
Chatbot WhatsApp endpoints. Port de /api/chatbot/* + webhook.
Webhook público bajo /api/chatbot/webhook (Meta Cloud API).
"""
from flask import Blueprint, request, jsonify

from extensions import db
from models import ChatbotConfig, ChatbotConversation, ChatbotMessage
import chatbot

chatbot_bp = Blueprint("chatbot", __name__)


# ── Webhook (público — sin auth) ───────────────────────────────────


@chatbot_bp.route("/webhook", methods=["GET"])
def webhook_verify():
    body, status = chatbot.handle_webhook_verification(request.args)
    return body, status


@chatbot_bp.route("/webhook", methods=["POST"])
def webhook_message():
    """Meta espera 200 inmediato. Procesamos sync (gevent worker yields)."""
    result = chatbot.handle_incoming_message(request.get_json(silent=True) or {})
    return jsonify(result), 200


# ── Admin: conversations ───────────────────────────────────────────


@chatbot_bp.route("/conversations", methods=["GET"])
def list_conversations():
    q = ChatbotConversation.query
    unit = request.args.get("unit")
    status = request.args.get("status")
    if unit:
        q = q.filter(ChatbotConversation.unit == unit)
    if status:
        q = q.filter(ChatbotConversation.status == status)
    rows = q.order_by(ChatbotConversation.updated_at.desc()).limit(200).all()
    return jsonify([r.to_dict() for r in rows])


@chatbot_bp.route("/conversations/<int:conv_id>", methods=["GET"])
def get_conversation(conv_id):
    conv = db.session.get(ChatbotConversation, conv_id)
    if not conv:
        return jsonify({"error": "not found"}), 404
    out = conv.to_dict()
    out["messages"] = [m.to_dict() for m in conv.messages]
    return jsonify(out)


@chatbot_bp.route("/conversations/<int:conv_id>/assign", methods=["PUT"])
def assign_conversation(conv_id):
    conv = db.session.get(ChatbotConversation, conv_id)
    if not conv:
        return jsonify({"error": "not found"}), 404
    data = request.get_json() or {}
    conv.assigned_to = data.get("user_id")
    db.session.commit()
    return jsonify(conv.to_dict())


@chatbot_bp.route("/stats", methods=["GET"])
def stats():
    return jsonify(chatbot.get_stats(unit=request.args.get("unit")))


# ── Admin: config CRUD ─────────────────────────────────────────────


@chatbot_bp.route("/config", methods=["GET"])
def list_configs():
    rows = ChatbotConfig.query.order_by(ChatbotConfig.unit).all()
    return jsonify([r.to_dict() for r in rows])


@chatbot_bp.route("/config", methods=["POST"])
def upsert_config():
    """Body: {unit, phone_number_id, wa_business_account_id, wa_access_token,
    webhook_verify_token, closer_user_id, active}"""
    data = request.get_json() or {}
    unit = data.get("unit")
    if not unit:
        return jsonify({"error": "unit requerido"}), 400
    cfg = db.session.get(ChatbotConfig, unit)
    if not cfg:
        cfg = ChatbotConfig(unit=unit)
        db.session.add(cfg)
    for fld in ("phone_number_id", "wa_business_account_id", "wa_access_token",
                "webhook_verify_token", "closer_user_id", "active"):
        if fld in data:
            setattr(cfg, fld, data[fld])
    db.session.commit()
    return jsonify(cfg.to_dict())

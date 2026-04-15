# blueprints/chat.py
import logging
import requests
from flask import Blueprint, request, jsonify, current_app
from extensions import db, socketio
from models import Lead, MensajeWhatsapp, DireccionMensaje

logger = logging.getLogger(__name__)
chat_bp = Blueprint("chat", __name__)


@chat_bp.route("/<uuid:lead_id>/mensajes", methods=["GET"])
def obtener_mensajes(lead_id):
    """Retorna el historial completo de mensajes de un lead."""
    lead = db.session.get(Lead, lead_id)
    if not lead:
        return jsonify({"error": "Lead no encontrado"}), 404

    mensajes = lead.mensajes.order_by(MensajeWhatsapp.timestamp.asc()).all()
    return jsonify([m.to_dict() for m in mensajes])


@chat_bp.route("/<uuid:lead_id>/enviar", methods=["POST"])
def enviar_mensaje(lead_id):
    """
    Envía un mensaje de texto al lead por WhatsApp.
    Intenta primero vía Baileys, si falla intenta Cloud API.

    Body esperado: { "contenido": "Hola, ¿en qué te puedo ayudar?" }
    """
    import os
    lead = db.session.get(Lead, lead_id)
    if not lead:
        return jsonify({"error": "Lead no encontrado"}), 404

    if not lead.telefono:
        return jsonify({"error": "El lead no tiene teléfono registrado"}), 400

    body      = request.get_json(silent=True) or {}
    contenido = body.get("contenido", "").strip()

    if not contenido:
        return jsonify({"error": "El mensaje no puede estar vacío"}), 400

    enviado = False
    wa_message_id = None

    # ── Intentar enviar vía Baileys ─────────────
    baileys_url = os.getenv("BAILEYS_URL", "")
    bot_secret = os.getenv("BOT_SECRET", "avantex-bot-2026")
    # Determinar sesión por marca del lead
    session_map = {
        "Aromatex": "aromatex", "Pestex": "pestex",
        "Weldex": "weldex", "Nexo": "nexo",
        "Aromatex Home": "aromatex_home",
    }
    session_id = session_map.get(lead.marca_interes, "aromatex")

    if baileys_url:
        try:
            resp = requests.post(f"{baileys_url}/api/send", json={
                "session_id": session_id,
                "telefono": lead.telefono,
                "contenido": contenido,
                "secret": bot_secret,
            }, timeout=10)
            if resp.ok:
                wa_message_id = resp.json().get("message_id")
                enviado = True
                # Marcar bot como transferido
                requests.post(f"{baileys_url}/api/bot/transfer", json={
                    "session_id": session_id,
                    "telefono": lead.telefono,
                    "secret": bot_secret,
                }, timeout=5)
        except requests.RequestException as e:
            logger.warning(f"Baileys no disponible, intentando Cloud API: {e}")

    # ── Fallback a Cloud API ─────────────
    if not enviado:
        wa_token = current_app.config["WHATSAPP_TOKEN"]
        phone_id = current_app.config["WHATSAPP_PHONE_ID"]
        if wa_token and phone_id:
            url = f"https://graph.facebook.com/v19.0/{phone_id}/messages"
            payload = {
                "messaging_product": "whatsapp",
                "to": lead.telefono.lstrip("+"),
                "type": "text",
                "text": {"body": contenido},
            }
            headers = {
                "Authorization": f"Bearer {wa_token}",
                "Content-Type": "application/json",
            }
            try:
                resp = requests.post(url, json=payload, headers=headers, timeout=10)
                resp.raise_for_status()
                wa_response = resp.json()
                wa_message_id = wa_response.get("messages", [{}])[0].get("id")
                enviado = True
            except requests.RequestException as e:
                logger.error(f"Error enviando mensaje WA Cloud API: {e}")

    if not enviado:
        return jsonify({"error": "No se pudo enviar el mensaje por WhatsApp"}), 502

    # ── Guardar en la BD ────────────────────────
    nuevo_mensaje = MensajeWhatsapp(
        lead_id         = lead.id,
        meta_message_id = wa_message_id,
        direccion       = DireccionMensaje.SALIENTE_VENDEDOR,
        contenido       = contenido,
    )
    db.session.add(nuevo_mensaje)
    db.session.commit()

    # ── Emitir a todos los clientes en la sala del lead ──
    socketio.emit("nuevo_mensaje", {
        "mensaje": nuevo_mensaje.to_dict(),
        "lead":    lead.to_dict(),
    }, room=f"lead_{lead.id}")

    return jsonify(nuevo_mensaje.to_dict()), 201


# ── Eventos SocketIO ────────────────────────────────────

@socketio.on("unirse_sala")
def unirse_sala(data):
    """
    El frontend emite este evento al abrir el chat de un lead.
    Permite recibir solo los mensajes de esa conversación.
    """
    from flask_socketio import join_room
    lead_id = data.get("lead_id")
    if lead_id:
        join_room(f"lead_{lead_id}")
        logger.debug(f"Cliente unido a sala lead_{lead_id}")


@socketio.on("salir_sala")
def salir_sala(data):
    from flask_socketio import leave_room
    lead_id = data.get("lead_id")
    if lead_id:
        leave_room(f"lead_{lead_id}")

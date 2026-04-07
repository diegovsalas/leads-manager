# blueprints/webhooks.py
import logging
from flask import Blueprint, request, jsonify, current_app
from extensions import db, socketio
from models import Lead, MensajeWhatsapp, DireccionMensaje, EtapaPipeline, OrigenLead

logger = logging.getLogger(__name__)
webhooks_bp = Blueprint("webhooks", __name__)


# ══════════════════════════════════════════════
# WEBHOOK DE META ADS — recibe nuevos leads de
# campañas de Facebook e Instagram
# ══════════════════════════════════════════════

@webhooks_bp.route("/meta", methods=["GET"])
def verificar_webhook_meta():
    """
    Meta envía un GET para verificar el endpoint antes de activarlo.
    Debes devolver el hub.challenge si el token coincide.
    """
    mode      = request.args.get("hub.mode")
    token     = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    verify_token = current_app.config["META_VERIFY_TOKEN"]

    if mode == "subscribe" and token == verify_token:
        logger.info("Webhook de Meta verificado correctamente.")
        return challenge, 200

    logger.warning("Intento de verificación fallido: token incorrecto.")
    return "Forbidden", 403


@webhooks_bp.route("/meta", methods=["POST"])
def recibir_lead_meta():
    """
    Recibe el payload JSON cuando un usuario llena un formulario
    de Lead Ads en Facebook o Instagram.
    """
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Payload inválido"}), 400

    logger.debug(f"Webhook Meta recibido: {data}")

    try:
        for entry in data.get("entry", []):
            for change in entry.get("changes", []):
                if change.get("field") == "leadgen":
                    _procesar_lead_meta(change["value"])

        return jsonify({"status": "ok"}), 200

    except Exception as e:
        logger.exception(f"Error procesando webhook de Meta: {e}")
        return jsonify({"error": "Error interno"}), 500


def _procesar_lead_meta(lead_data: dict):
    """
    Extrae los campos del formulario, crea el Lead y lo asigna
    automáticamente al vendedor correcto vía Round-Robin.
    Emite evento SocketIO para notificar al frontend en tiempo real.
    """
    meta_lead_id = lead_data.get("leadgen_id")

    # Evitar duplicados
    if Lead.query.filter_by(meta_lead_id=meta_lead_id).first():
        logger.info(f"Lead de Meta ya existe: {meta_lead_id}")
        return

    # Parsear los field_data del formulario
    campos = {
        item["name"]: item["values"][0]
        for item in lead_data.get("field_data", [])
        if item.get("values")
    }

    nombre   = campos.get("full_name") or campos.get("nombre", "Sin nombre")
    telefono = campos.get("phone_number") or campos.get("telefono")

    # Detectar marca de interés (puede venir como campo personalizado del form)
    marca = campos.get("marca_interes") or campos.get("brand", "")

    # ── Intentar asignación automática Round-Robin ──
    from asignacion import asignar_lead_comercial

    try:
        nuevo_lead = asignar_lead_comercial({
            "telefono":      telefono,
            "nombre":        nombre,
            "origen":        OrigenLead.META_ADS.value,
            "marca_interes": marca,
            "meta_lead_id":  meta_lead_id,
            "meta_form_id":  lead_data.get("form_id"),
            "meta_ad_id":    lead_data.get("ad_id"),
            "meta_campaign": lead_data.get("campaign_id"),
        })
        logger.info(
            f"Lead Meta asignado: {nuevo_lead.id} → "
            f"{nuevo_lead.usuario_asignado.nombre if nuevo_lead.usuario_asignado else 'Sin asignar'}"
        )
    except ValueError:
        # No hay vendedores disponibles — crear sin asignar
        nuevo_lead = Lead(
            telefono       = telefono,
            nombre         = nombre,
            origen         = OrigenLead.META_ADS,
            marca_interes  = marca,
            etapa_pipeline = EtapaPipeline.NUEVO_LEAD,
            meta_lead_id   = meta_lead_id,
            meta_form_id   = lead_data.get("form_id"),
            meta_ad_id     = lead_data.get("ad_id"),
            meta_campaign  = lead_data.get("campaign_id"),
        )
        db.session.add(nuevo_lead)
        db.session.commit()
        logger.warning(f"Lead Meta creado SIN asignar (sin vendedores disponibles): {nuevo_lead.id}")

    # ── Notificar al frontend en tiempo real ──
    socketio.emit("nuevo_lead", nuevo_lead.to_dict())


# ══════════════════════════════════════════════
# WEBHOOK DE WHATSAPP CLOUD API — recibe mensajes
# que los leads envían a tu número de negocio
# ══════════════════════════════════════════════

@webhooks_bp.route("/whatsapp", methods=["GET"])
def verificar_webhook_whatsapp():
    """Misma lógica de verificación — WhatsApp usa el mismo protocolo de Meta."""
    return verificar_webhook_meta()


@webhooks_bp.route("/whatsapp", methods=["POST"])
def recibir_mensaje_whatsapp():
    """Recibe mensajes entrantes de WhatsApp Cloud API."""
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Payload inválido"}), 400

    logger.debug(f"Webhook WhatsApp recibido: {data}")

    try:
        for entry in data.get("entry", []):
            for change in entry.get("changes", []):
                if change.get("field") == "messages":
                    _procesar_mensaje_whatsapp(change["value"])

        return jsonify({"status": "ok"}), 200

    except Exception as e:
        logger.exception(f"Error procesando webhook de WhatsApp: {e}")
        return jsonify({"error": "Error interno"}), 500


def _procesar_mensaje_whatsapp(value: dict):
    """
    Guarda el mensaje en la BD y emite evento SocketIO hacia el frontend.
    Si el contacto no existe como Lead, lo crea con asignación Round-Robin.
    """
    from asignacion import asignar_lead_comercial

    mensajes_wa = value.get("messages", [])
    contactos   = {c["wa_id"]: c for c in value.get("contacts", [])}

    for msg in mensajes_wa:
        wa_message_id = msg.get("id")

        # Evitar duplicados
        if MensajeWhatsapp.query.filter_by(meta_message_id=wa_message_id).first():
            logger.info(f"Mensaje WA ya procesado: {wa_message_id}")
            continue

        telefono_wa = msg.get("from")
        telefono    = f"+{telefono_wa}"

        # ── Buscar o crear el Lead ──────────────
        lead = Lead.query.filter_by(telefono=telefono).first()
        if not lead:
            contacto  = contactos.get(telefono_wa, {})
            nombre_wa = contacto.get("profile", {}).get("name", telefono)

            try:
                lead = asignar_lead_comercial({
                    "telefono":      telefono,
                    "nombre":        nombre_wa,
                    "origen":        OrigenLead.WHATSAPP_ORGANICO.value,
                    "marca_interes": "",
                })
            except ValueError:
                # Sin vendedores disponibles — crear sin asignar
                lead = Lead(
                    nombre         = nombre_wa,
                    telefono       = telefono,
                    origen         = OrigenLead.WHATSAPP_ORGANICO,
                    etapa_pipeline = EtapaPipeline.NUEVO_LEAD,
                )
                db.session.add(lead)
                db.session.flush()

            logger.info(f"Lead creado desde WhatsApp: {lead.nombre} ({telefono})")

        # ── Extraer contenido según el tipo ────
        tipo      = msg.get("type", "text")
        contenido = _extraer_contenido(msg, tipo)

        nuevo_mensaje = MensajeWhatsapp(
            lead_id         = lead.id,
            meta_message_id = wa_message_id,
            direccion       = DireccionMensaje.ENTRANTE,
            contenido       = contenido,
        )
        # ── Registrar respuesta (detiene cadencia) ──
        from datetime import datetime, timezone as tz
        lead.respondio_ultimo_contacto = True
        lead.fecha_ultimo_contacto = datetime.now(tz.utc)

        db.session.add(nuevo_mensaje)
        db.session.commit()

        logger.info(f"Mensaje WA guardado: lead={lead.id}, tipo={tipo}, cadencia detenida")

        # ── Emitir evento SocketIO al frontend ──
        socketio.emit("nuevo_mensaje", {
            "mensaje": nuevo_mensaje.to_dict(),
            "lead":    lead.to_dict(),
        }, room=f"lead_{lead.id}")

        socketio.emit("mensaje_global", {
            "lead_id":     str(lead.id),
            "lead_nombre": lead.nombre,
            "preview":     contenido[:80],
        })


def _extraer_contenido(msg: dict, tipo: str) -> str:
    """Extrae el texto o descripción del mensaje según su tipo."""
    extractores = {
        "text":     lambda m: m.get("text", {}).get("body", ""),
        "image":    lambda m: m.get("image", {}).get("caption", "[Imagen]"),
        "audio":    lambda m: "[Audio]",
        "document": lambda m: m.get("document", {}).get("filename", "[Documento]"),
        "video":    lambda m: m.get("video", {}).get("caption", "[Video]"),
        "location": lambda m: f"[Ubicación: {m.get('location', {}).get('latitude')}, {m.get('location', {}).get('longitude')}]",
        "sticker":  lambda m: "[Sticker]",
    }
    return extractores.get(tipo, lambda m: "[Mensaje no soportado]")(msg)

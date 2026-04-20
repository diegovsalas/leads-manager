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
    Guarda el mensaje en la BD, ejecuta bot presales si aplica,
    y emite evento SocketIO hacia el frontend.
    """
    from asignacion import asignar_lead_comercial
    from datetime import datetime, timezone as tz

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
        is_new = lead is None
        if not lead:
            contacto  = contactos.get(telefono_wa, {})
            nombre_wa = contacto.get("profile", {}).get("name", telefono)

            lead = Lead(
                nombre         = nombre_wa,
                telefono       = telefono,
                origen         = OrigenLead.WHATSAPP_ORGANICO,
                etapa_pipeline = EtapaPipeline.NUEVO_LEAD,
                bot_step       = "waiting_name",
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
        lead.respondio_ultimo_contacto = True
        lead.fecha_ultimo_contacto = datetime.now(tz.utc)
        db.session.add(nuevo_mensaje)
        db.session.commit()

        logger.info(f"Mensaje WA guardado: lead={lead.id}, tipo={tipo}")

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

        # ── Bot presales automático ──
        try:
            db.session.refresh(lead)  # re-leer estado actual del bot
            if is_new or (lead.bot_step is None and lead.usuario_asignado_id is None):
                lead.bot_step = "waiting_name"
                db.session.commit()
                bienvenida = "Hola! Bienvenido a *Grupo Avantex*.\nSomos especialistas en servicios para tu negocio.\n\n¿Con quién tengo el gusto?"
                _bot_send(telefono_wa, bienvenida)
                _save_bot_msg(lead, bienvenida)
            elif lead.bot_step and lead.bot_step != "transferred":
                _handle_bot_step(lead, contenido, telefono_wa)
        except Exception as e:
            logger.exception(f"Error en bot presales: {e}")


def _handle_bot_step(lead, contenido, telefono_wa):
    """Maneja el flujo del bot presales paso a paso."""
    from asignacion import asignar_lead_comercial

    step = lead.bot_step
    texto = contenido.strip()
    logger.info(f"Bot step={step} para lead={lead.id}, contenido={texto[:50]}")

    if step == "waiting_name":
        lead.nombre = texto
        lead.bot_step = "waiting_empresa"
        db.session.commit()
        resp = f"Mucho gusto *{texto}*. ¿De qué empresa nos contacta?"
        _bot_send(telefono_wa, resp)
        _save_bot_msg(lead, resp)

    elif step == "waiting_empresa":
        lead.empresa_nombre = texto
        lead.bot_step = "waiting_sucursales"
        db.session.commit()
        resp = "¿Cuántas sucursales tienen?"
        _bot_send(telefono_wa, resp)
        _save_bot_msg(lead, resp)

    elif step == "waiting_sucursales":
        try:
            lead.num_sucursales = int("".join(c for c in texto if c.isdigit()) or "0")
        except Exception:
            lead.num_sucursales = 0
        lead.bot_step = "waiting_estado"
        db.session.commit()
        resp = "¿En qué estado o ciudad se encuentran?"
        _bot_send(telefono_wa, resp)
        _save_bot_msg(lead, resp)

    elif step == "waiting_estado":
        from asignacion import normalizar_estado
        lead.estado_cliente = normalizar_estado(texto)
        lead.bot_step = "waiting_servicio"
        db.session.commit()
        resp = ("¿Qué servicio le interesa?\n\n"
                "1. Aromatización de espacios\n"
                "2. Control de plagas\n"
                "3. Limpieza y desinfección\n"
                "4. Soldadura industrial\n"
                "5. Marketing digital\n"
                "6. Otro")
        _bot_send(telefono_wa, resp)
        _save_bot_msg(lead, resp)

    elif step == "waiting_servicio":
        servicios_map = {
            "1": ("Aromatización de espacios", "Aromatex"),
            "2": ("Control de plagas", "Pestex"),
            "3": ("Limpieza y desinfección", "Pestex"),
            "4": ("Soldadura industrial", "Weldex"),
            "5": ("Marketing digital", "Nexo"),
            "6": (texto, ""),
        }
        servicio, marca = servicios_map.get(texto, (texto, ""))
        lead.marca_interes = marca
        lead.bot_step = "transferred"

        # Asignar vendedor por Round-Robin
        try:
            from models import Usuario
            candidatos = (
                Usuario.query.filter(
                    Usuario.en_turno.is_(True),
                    db.or_(
                        Usuario.especialidad_marca.any(marca),
                        Usuario.especialidad_marca.any("Todas"),
                    ),
                ).order_by(Usuario.ultimo_lead_asignado.asc().nullsfirst()).all()
            )
            if candidatos:
                from datetime import datetime, timezone
                vendedor = candidatos[0]
                lead.usuario_asignado_id = vendedor.id
                vendedor.ultimo_lead_asignado = datetime.now(timezone.utc)
                nombre_vendedor = vendedor.nombre.split(" ")[0]
            else:
                nombre_vendedor = "un asesor"
        except Exception as e:
            logger.error(f"Error asignando vendedor: {e}")
            nombre_vendedor = "un asesor"

        db.session.commit()

        resp = f"Gracias! *{nombre_vendedor}* será tu asesor y te contactará en los próximos minutos por este mismo chat."
        _bot_send(telefono_wa, resp)
        _save_bot_msg(lead, resp)

        logger.info(f"Bot completó calificación: lead={lead.id}, marca={marca}, vendedor={nombre_vendedor}")


def _bot_send(telefono_wa, text):
    """Envía un mensaje de WhatsApp vía Cloud API."""
    import requests, os
    wa_token = os.getenv("WHATSAPP_TOKEN", "")
    phone_id = os.getenv("WHATSAPP_PHONE_ID", "")
    if not wa_token or not phone_id:
        logger.warning("WHATSAPP_TOKEN o WHATSAPP_PHONE_ID no configurados")
        return
    url = f"https://graph.facebook.com/v25.0/{phone_id}/messages"
    try:
        resp = requests.post(url, json={
            "messaging_product": "whatsapp",
            "to": telefono_wa,
            "type": "text",
            "text": {"body": text},
        }, headers={
            "Authorization": f"Bearer {wa_token}",
            "Content-Type": "application/json",
        }, timeout=10)
        if not resp.ok:
            logger.error(f"Error enviando bot msg: {resp.status_code} {resp.text}")
    except Exception as e:
        logger.error(f"Error enviando bot msg: {e}")


def _save_bot_msg(lead, text):
    """Guarda mensaje del bot en la BD y emite SocketIO."""
    msg = MensajeWhatsapp(
        lead_id=lead.id,
        direccion=DireccionMensaje.SALIENTE_BOT,
        contenido=text,
    )
    db.session.add(msg)
    db.session.commit()
    socketio.emit("nuevo_mensaje", {
        "mensaje": msg.to_dict(),
        "lead": lead.to_dict(),
    }, room=f"lead_{lead.id}")



# ══════════════════════════════════════════════
# WEBHOOK BAILEYS — recibe mensajes del bot Node.js
# ══════════════════════════════════════════════

@webhooks_bp.route("/baileys", methods=["POST"])
def recibir_mensaje_baileys():
    """Recibe mensajes desde el microservicio Baileys."""
    import os
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Payload inválido"}), 400

    secret = data.get("secret", "")
    if secret != os.getenv("BOT_SECRET", "avantex-bot-2026"):
        return jsonify({"error": "No autorizado"}), 403

    telefono = data.get("telefono", "")
    nombre = data.get("nombre", "")
    contenido = data.get("contenido", "")
    session_id = data.get("session_id", "")
    lead_data = data.get("lead_data")
    direccion = data.get("direccion", "entrante")

    if not telefono:
        return jsonify({"error": "telefono requerido"}), 400

    # ── Buscar o crear Lead ──
    lead = Lead.query.filter_by(telefono=telefono).first()

    if not lead and lead_data:
        # Bot completó calificación — crear lead calificado
        from asignacion import asignar_lead_comercial
        marca = lead_data.get("marca", "")

        try:
            lead = asignar_lead_comercial({
                "telefono": telefono,
                "nombre": lead_data.get("nombre", nombre),
                "origen": OrigenLead.WHATSAPP_ORGANICO.value,
                "marca_interes": marca,
                "estado": lead_data.get("estado", ""),
            })
            # Guardar datos de calificación
            lead.empresa_nombre = lead_data.get("empresa", "")
            suc = lead_data.get("sucursales", "0") or "0"
            lead.num_sucursales = int("".join(c for c in str(suc) if c.isdigit()) or "0")
            db.session.commit()

            logger.info(f"Lead Baileys calificado: {lead.nombre} → {lead.usuario_asignado.nombre if lead.usuario_asignado else 'Sin asignar'}")

            # Notificar al vendedor por WhatsApp
            _notificar_vendedor_baileys(lead, session_id, lead_data)

        except ValueError:
            lead = Lead(
                nombre=lead_data.get("nombre", nombre),
                telefono=telefono,
                origen=OrigenLead.WHATSAPP_ORGANICO,
                etapa_pipeline=EtapaPipeline.NUEVO_LEAD,
                marca_interes=lead_data.get("marca", ""),
            )
            db.session.add(lead)
            db.session.commit()

    elif not lead:
        # Mensaje sin calificación completada — crear lead básico
        lead = Lead(
            nombre=nombre or telefono,
            telefono=telefono,
            origen=OrigenLead.WHATSAPP_ORGANICO,
            etapa_pipeline=EtapaPipeline.NUEVO_LEAD,
        )
        db.session.add(lead)
        db.session.flush()

    # ── Guardar mensaje ──
    msg_dir = DireccionMensaje.SALIENTE_BOT if direccion == "bot" else DireccionMensaje.ENTRANTE
    nuevo_mensaje = MensajeWhatsapp(
        lead_id=lead.id,
        direccion=msg_dir,
        contenido=contenido,
    )

    from datetime import datetime, timezone as tz
    if direccion != "bot":
        lead.respondio_ultimo_contacto = True
        lead.fecha_ultimo_contacto = datetime.now(tz.utc)

    db.session.add(nuevo_mensaje)
    db.session.commit()

    # ── Emitir SocketIO ──
    socketio.emit("nuevo_mensaje", {
        "mensaje": nuevo_mensaje.to_dict(),
        "lead": lead.to_dict(),
    }, room=f"lead_{lead.id}")

    socketio.emit("mensaje_global", {
        "lead_id": str(lead.id),
        "lead_nombre": lead.nombre,
        "preview": contenido[:80],
    })

    return jsonify({"ok": True, "lead_id": str(lead.id)}), 200


def _notificar_vendedor_baileys(lead, session_id, lead_data):
    """Envía notificación por WhatsApp al vendedor asignado."""
    import os, requests as http
    vendedor = lead.usuario_asignado
    if not vendedor or not vendedor.telefono:
        return

    bot_url = os.getenv("BAILEYS_URL", "http://localhost:3001")
    bot_secret = os.getenv("BOT_SECRET", "avantex-bot-2026")

    mensaje = (
        f"🔔 *Nuevo lead asignado*\n\n"
        f"👤 {lead_data.get('nombre', '')}\n"
        f"🏢 {lead_data.get('empresa', '')}\n"
        f"📍 {lead_data.get('sucursales', '')} sucursales\n"
        f"🎯 {lead_data.get('servicio', '')}\n"
        f"📱 {lead.telefono}\n"
        f"🏷️ {lead_data.get('marca', '')}\n\n"
        f"📋 Ver en CRM: https://leads-manager-avantex.onrender.com"
    )

    try:
        http.post(f"{bot_url}/api/send", json={
            "session_id": session_id,
            "telefono": vendedor.telefono,
            "contenido": mensaje,
            "secret": bot_secret,
        }, timeout=10)
    except Exception as e:
        logger.warning(f"No se pudo notificar al vendedor: {e}")


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

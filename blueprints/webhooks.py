# blueprints/webhooks.py
import hashlib
import hmac
import logging
import os
from flask import Blueprint, request, jsonify, current_app
from extensions import db, socketio, limiter
from models import Lead, MensajeWhatsapp, DireccionMensaje, EtapaPipeline, OrigenLead

logger = logging.getLogger(__name__)
webhooks_bp = Blueprint("webhooks", __name__)


def _verify_meta_signature(app_secret: str, raw_body: bytes, signature_header: str) -> bool:
    """SECURITY-2026-07-14: valida X-Hub-Signature-256 para confirmar que el
    POST viene de Meta y no de un tercero que descubrió la URL."""
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    provided = signature_header.split("=", 1)[1]
    return hmac.compare_digest(expected, provided)


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
    app_secret = current_app.config.get("META_APP_SECRET")
    if not app_secret:
        logger.error("META_APP_SECRET no configurada — rechazando webhook de Meta.")
        return jsonify({"error": "Webhook no configurado"}), 503
    signature = request.headers.get("X-Hub-Signature-256", "")
    if not _verify_meta_signature(app_secret, request.get_data(), signature):
        logger.warning("Webhook de Meta con firma inválida o ausente — rechazado.")
        return jsonify({"error": "Firma inválida"}), 401

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
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Payload inválido"}), 400

    bot_secret = os.getenv("BOT_SECRET")
    secret = data.get("secret", "")
    if not bot_secret or not hmac.compare_digest(str(secret), bot_secret):
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


# ══════════════════════════════════════════════
# TEST — Meta Conversions API (temporal)
# ══════════════════════════════════════════════
@webhooks_bp.route("/meta/test-capi", methods=["GET"])
def test_meta_capi():
    """
    Dispara un evento de prueba a Meta CAPI.
    Uso: /webhook/meta/test-capi?marca=aromatex&test_code=TEST12345
    """
    from meta_conversions import send_conversion_event, _resolve_pixel

    marca = request.args.get("marca", "aromatex")
    test_code = request.args.get("test_code", "")

    pixel_cfg = _resolve_pixel(marca)
    if not pixel_cfg:
        return jsonify({"error": f"No hay pixel configurado para marca '{marca}'"}), 400

    # Lead ficticio para la prueba
    class FakeLead:
        id = "test-0000"
        nombre = "Test Lead"
        correo = "test@avantex.com"
        telefono = "5551234567"
        meta_lead_id = None
        meta_campaign = "test_campaign"
        marca_interes = marca

    result = send_conversion_event(
        FakeLead(), "Lead", pixel_cfg,
        value=1000.0, test_event_code=test_code,
    )
    return jsonify({"status": "sent", "marca": marca, "pixel": pixel_cfg["pixel_id"], "meta_response": result})


@webhooks_bp.route("/meta/subscribe-page", methods=["GET"])
def subscribe_page_leadgen():
    """
    Suscribe una Page al webhook de leadgen de esta App.
    Uso: /webhook/meta/subscribe-page?page_id=153702277822462
    Intercambia el system user token por un Page token automáticamente.
    """
    import os, requests as http

    page_id = request.args.get("page_id", "").strip()
    if not page_id:
        return jsonify({"error": "Falta page_id"}), 400

    system_token = os.getenv("META_PAGE_TOKEN", "")
    if not system_token:
        return jsonify({"error": "Falta META_PAGE_TOKEN en env vars"}), 400

    api_version = os.getenv("META_API_VERSION", "v19.0")
    base = f"https://graph.facebook.com/{api_version}"

    # Paso 1: intercambiar system user token por page access token
    token_resp = http.get(f"{base}/{page_id}", params={
        "fields": "access_token,name",
        "access_token": system_token,
    }, timeout=15)
    token_data = token_resp.json()

    if "access_token" not in token_data:
        return jsonify({
            "error": "No se pudo obtener Page token",
            "meta_response": token_data,
            "help": "El system user necesita permisos de Page (manage page + leads)",
        }), 400

    page_token = token_data["access_token"]
    page_name = token_data.get("name", "")

    # Paso 2: suscribir la page al webhook de leadgen
    resp = http.post(f"{base}/{page_id}/subscribed_apps", json={
        "subscribed_fields": "leadgen",
        "access_token": page_token,
    }, timeout=15)

    result = resp.json()
    return jsonify({
        "page_id": page_id,
        "page_name": page_name,
        "status_code": resp.status_code,
        "meta_response": result,
    })


@webhooks_bp.route("/meta/poll-leads", methods=["GET"])
def poll_leads():
    """
    Consulta Meta por nuevos leads vía Graph API (alternativa al webhook).
    Uso: /webhook/meta/poll-leads
    """
    from meta_lead_polling import poll_and_create_leads
    stats = poll_and_create_leads()
    return jsonify(stats)


@webhooks_bp.route("/linkedin/poll-leads", methods=["GET"])
def poll_linkedin_leads():
    """
    Consulta LinkedIn por nuevos leads vía Marketing API.
    Uso: /webhook/linkedin/poll-leads
    """
    from linkedin_lead_polling import poll_and_create_leads
    stats = poll_and_create_leads()
    return jsonify(stats)


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


# ══════════════════════════════════════════════
# WEBHOOKS DE SHOPIFY — tienda Weldu (welduapp.com)
#
# FEAT-2026-09-29. La tienda es de la UN Weldex, que ya existe en el CRM con
# sus vendedores y su tabulador de comisiones.
#
# Shopify firma distinto que Meta: el HMAC-SHA256 va en base64, no en hex, y
# se calcula sobre el cuerpo crudo. Usar request.get_json() antes de validar
# rompe la firma, porque el JSON reserializado no es byte a byte el original.
# ══════════════════════════════════════════════

SHOPIFY_MARCA = os.getenv("SHOPIFY_MARCA", "Weldex")

# Correos de alta claramente automatizados. Ya hay basura de este tipo dada de
# alta en la tienda (ej. "123HannahOunengxfmevbqq.dpn@inscrlab.com"), y sin
# filtro entraria al pipe de los dos vendedores de Weldex.
_SHOPIFY_DOMINIOS_SPAM = (
    "inscrlab.com", "mailinator.com", "tempmail", "guerrillamail",
    "10minutemail", "yopmail.com", "trashmail",
)


def _verify_shopify_hmac(secret: str, raw_body: bytes, header: str) -> bool:
    """Valida X-Shopify-Hmac-Sha256 (HMAC-SHA256 en base64 del cuerpo crudo)."""
    if not header:
        return False
    import base64
    digest = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).digest()
    esperado = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(esperado, header)


def _shopify_guard():
    """Valida firma y tienda. Devuelve (payload, None) o (None, respuesta)."""
    secret = os.getenv("SHOPIFY_WEBHOOK_SECRET", "").strip()
    if not secret:
        logger.error("SHOPIFY_WEBHOOK_SECRET no configurada — rechazando webhook.")
        return None, (jsonify({"error": "Webhook no configurado"}), 503)

    raw = request.get_data()
    if not _verify_shopify_hmac(secret, raw, request.headers.get("X-Shopify-Hmac-Sha256", "")):
        logger.warning("Webhook de Shopify con firma invalida — rechazado.")
        return None, (jsonify({"error": "Firma invalida"}), 401)

    # Que la firma sea valida no dice de que tienda viene: si mañana hay otra
    # tienda con el mismo secreto, sus clientes entrarian como Weldex.
    dominio_ok = os.getenv("SHOPIFY_SHOP_DOMAIN", "").strip().lower()
    dominio = (request.headers.get("X-Shopify-Shop-Domain") or "").strip().lower()
    if dominio_ok and dominio and dominio != dominio_ok:
        logger.warning("Webhook de Shopify de otra tienda (%s) — ignorado.", dominio)
        return None, (jsonify({"status": "ignorado"}), 200)

    payload = request.get_json(silent=True)
    if payload is None:
        return None, (jsonify({"error": "Payload invalido"}), 400)
    return payload, None


def _shopify_datos_contacto(cliente: dict) -> dict:
    """Normaliza nombre, email y telefono de un customer/checkout/order."""
    nombre = " ".join(
        p for p in ((cliente.get("first_name") or "").strip(),
                    (cliente.get("last_name") or "").strip()) if p
    ).strip()
    email = (cliente.get("email") or "").strip().lower()
    telefono = (cliente.get("phone") or "").strip()
    return {"nombre": nombre, "email": email, "telefono": telefono}


def _shopify_es_spam(email: str) -> bool:
    if not email:
        return False
    low = email.lower()
    return any(d in low for d in _SHOPIFY_DOMINIOS_SPAM)


def _shopify_buscar_lead(shopify_id_campo: str, shopify_id: str,
                         email: str, telefono: str):
    """Busca un lead ya existente: primero por el id de Shopify, luego por
    telefono y por email. El mismo cliente llega por varios eventos y no
    queremos una tarjeta por evento."""
    if shopify_id:
        lead = Lead.query.filter(getattr(Lead, shopify_id_campo) == str(shopify_id)).first()
        if lead:
            return lead
    if telefono:
        lead = Lead.query.filter_by(telefono=telefono).first()
        if lead:
            return lead
    if email:
        lead = Lead.query.filter_by(email=email).first()
        if lead:
            return lead
    return None


def _shopify_alta_lead(datos: dict, extra: dict, nota: str):
    """Crea el lead si trae datos suficientes, asignandolo por Round-Robin.

    Regla acordada: un correo suelto no basta. La tienda da de alta un cliente
    por cada suscripcion al boletin, y la mayoria llega sin nombre ni telefono;
    meterlos todos al pipe de los dos vendedores de Weldex seria ruido, no
    prospectos. Se exige al menos nombre o telefono.
    """
    nombre, email, telefono = datos["nombre"], datos["email"], datos["telefono"]

    if _shopify_es_spam(email):
        logger.info("[shopify] alta descartada por correo de spam: %s", email)
        return None
    if not nombre and not telefono:
        logger.info("[shopify] alta descartada, solo correo sin nombre ni telefono: %s", email)
        return None

    base = {
        "nombre":        nombre or (email.split("@")[0] if email else "Sin nombre"),
        "telefono":      telefono or None,
        "email":         email or None,
        "origen":        OrigenLead.WEB.value,
        "marca_interes": SHOPIFY_MARCA,
        "notas":         nota,
        **extra,
    }

    from asignacion import asignar_lead_comercial
    try:
        lead = asignar_lead_comercial(base)
        logger.info("[shopify] lead %s asignado a %s", lead.id,
                    lead.usuario_asignado.nombre if lead.usuario_asignado else "nadie")
    except ValueError:
        # Sin vendedores en turno para la UN: se crea igual, sin dueño. Perder
        # el prospecto seria peor que dejarlo sin asignar.
        base.pop("origen", None)
        base.pop("estado", None)
        lead = Lead(origen=OrigenLead.WEB,
                    etapa_pipeline=EtapaPipeline.NUEVO_LEAD, **base)
        db.session.add(lead)
        db.session.commit()
        logger.warning("[shopify] lead %s creado SIN asignar (no hay vendedores de %s)",
                       lead.id, SHOPIFY_MARCA)
    socketio.emit("nuevo_lead", lead.to_dict())
    return lead


def _shopify_customer(payload: dict, actualizar: bool):
    """customers/create y customers/update."""
    cid = str(payload.get("id") or "")
    datos = _shopify_datos_contacto(payload)
    lead = _shopify_buscar_lead("shopify_customer_id", cid, datos["email"], datos["telefono"])

    if lead:
        # Enriquecer sin pisar: si el vendedor ya corrigio un dato a mano, el
        # webhook no debe deshacerlo. Solo se rellenan huecos.
        if not lead.shopify_customer_id and cid:
            lead.shopify_customer_id = cid
        if not lead.email and datos["email"]:
            lead.email = datos["email"]
        if not lead.telefono and datos["telefono"]:
            lead.telefono = datos["telefono"]
        if datos["nombre"] and (not lead.nombre or lead.nombre == "Sin nombre"):
            lead.nombre = datos["nombre"]
        db.session.commit()
        return {"accion": "actualizado", "lead_id": str(lead.id)}

    if actualizar:
        # Un update de alguien que nunca paso el filtro de alta no deberia
        # colarlo por la puerta de atras; se evalua con la misma regla.
        pass

    nota = f"Alta en la tienda Weldu (Shopify customer {cid})."
    if payload.get("accepts_marketing") or payload.get("email_marketing_consent"):
        nota += " Acepto marketing."
    lead = _shopify_alta_lead(datos, {"shopify_customer_id": cid or None}, nota)
    return {"accion": "creado" if lead else "descartado",
            "lead_id": str(lead.id) if lead else None}


def _shopify_checkout(payload: dict):
    """checkouts/create y checkouts/update — carrito de alta intencion."""
    chid = str(payload.get("id") or "")
    cliente = payload.get("customer") or {}
    datos = _shopify_datos_contacto(cliente)
    # El checkout trae email y telefono propios aunque no haya customer.
    datos["email"] = datos["email"] or (payload.get("email") or "").strip().lower()
    datos["telefono"] = datos["telefono"] or (payload.get("phone") or "").strip()

    lead = _shopify_buscar_lead("shopify_checkout_id", chid, datos["email"], datos["telefono"])
    if lead:
        if not lead.shopify_checkout_id and chid:
            lead.shopify_checkout_id = chid
            db.session.commit()
        return {"accion": "ya_existia", "lead_id": str(lead.id)}

    total = payload.get("total_price") or 0
    nota = (f"Carrito sin terminar en Weldu por ${total} "
            f"(Shopify checkout {chid}). Prospecto de alta intencion.")
    lead = _shopify_alta_lead(datos, {"shopify_checkout_id": chid or None}, nota)
    return {"accion": "creado" if lead else "descartado",
            "lead_id": str(lead.id) if lead else None}


def _shopify_order(payload: dict):
    """orders/create — la venta se concreto en la tienda.

    Cierra el lead como ganado por el monto de la orden, usando el MISMO
    cerrar_lead_core que la pantalla. Se clasifica servicio_unico/eventual
    porque el catalogo de Weldu son visitas de diagnostico, compras de una
    sola vez, no suscripciones: el default del core (suscripcion_nueva /
    recurrente) calcularia la comision sobre otra base.
    """
    oid = str(payload.get("id") or "")
    cliente = payload.get("customer") or {}
    datos = _shopify_datos_contacto(cliente)
    datos["email"] = datos["email"] or (payload.get("email") or "").strip().lower()
    datos["telefono"] = datos["telefono"] or (payload.get("phone") or "").strip()

    if oid and Lead.query.filter_by(shopify_order_id=oid).first():
        return {"accion": "orden_ya_procesada"}

    try:
        monto = float(payload.get("total_price") or 0)
    except (TypeError, ValueError):
        monto = 0.0
    folio = payload.get("name") or oid

    lead = _shopify_buscar_lead("shopify_customer_id", str(cliente.get("id") or ""),
                                datos["email"], datos["telefono"])
    if not lead:
        # Quien compra deja nombre y datos de envio, asi que pasa el filtro de
        # alta aunque el suscriptor del boletin no lo hiciera.
        lead = _shopify_alta_lead(
            datos, {"shopify_order_id": oid or None},
            f"Compra en Weldu {folio} por ${monto:,.2f}.")
        if not lead:
            logger.warning("[shopify] orden %s sin datos suficientes para crear lead", folio)
            return {"accion": "descartado"}

    lead.shopify_order_id = oid or None
    nota_orden = f"Orden {folio} de Weldu por ${monto:,.2f}."
    lead.notas = f"{lead.notas}\n{nota_orden}".strip() if lead.notas else nota_orden
    if monto and not lead.valor_estimado:
        lead.valor_estimado = monto
    db.session.commit()

    if lead.etapa_pipeline == EtapaPipeline.CIERRE_GANADO:
        return {"accion": "lead_ya_cerrado", "lead_id": str(lead.id)}

    from blueprints.leads import cerrar_lead_core
    out, status = cerrar_lead_core(lead, {
        "unidad":              lead.marca_interes or SHOPIFY_MARCA,
        "sale_type":           "servicio_unico",
        "sale_category":       "eventual",
        "mensualidad_cerrada": monto,
        "total_amount":        monto,
    }, quien_cierra=str(lead.usuario_asignado_id) if lead.usuario_asignado_id else None)

    if status >= 400:
        # No se tumba el webhook: la orden ya quedo anotada en el lead y
        # Shopify reintentaria en vano. Queda el registro para cerrarlo a mano.
        logger.warning("[shopify] orden %s no pudo cerrarse (%s): %s",
                       folio, status, out.get("error"))
        return {"accion": "anotado_sin_cerrar", "lead_id": str(lead.id),
                "motivo": out.get("error")}
    return {"accion": "cerrado_ganado", "lead_id": str(lead.id),
            "sale_id": out.get("sale_id")}


@webhooks_bp.route("/shopify", methods=["POST"])
def recibir_shopify():
    """Punto unico para todos los topics. Shopify dice cual en X-Shopify-Topic.

    Siempre responde 200 salvo firma invalida: un 500 hace que Shopify
    reintente 19 veces y termine desactivando el webhook. Los errores se
    registran y se responden como procesados.
    """
    payload, error = _shopify_guard()
    if error:
        return error

    topic = (request.headers.get("X-Shopify-Topic") or "").strip().lower()
    logger.info("[shopify] topic=%s id=%s", topic, payload.get("id"))

    try:
        if topic == "customers/create":
            resultado = _shopify_customer(payload, actualizar=False)
        elif topic == "customers/update":
            resultado = _shopify_customer(payload, actualizar=True)
        elif topic in ("checkouts/create", "checkouts/update"):
            resultado = _shopify_checkout(payload)
        elif topic == "orders/create":
            resultado = _shopify_order(payload)
        else:
            logger.info("[shopify] topic no manejado: %s", topic)
            return jsonify({"status": "ignorado", "topic": topic}), 200
    except Exception as e:
        db.session.rollback()
        logger.exception("[shopify] error procesando %s: %s", topic, e)
        return jsonify({"status": "error_registrado"}), 200

    return jsonify({"status": "ok", "topic": topic, **resultado}), 200


# ── Formulario de contacto del tema ────────────────────────────────
#
# Shopify no dispara webhook cuando alguien envia el formulario de contacto,
# asi que el tema publica aqui por JavaScript. Este endpoint es PUBLICO a
# proposito: la alternativa era poner una API key en el codigo del tema, que
# es codigo fuente visible para cualquiera —quien la leyera podria crear
# leads a voluntad, y usarla contra el resto de la API—. Se protege con
# limite por IP, campo trampa y validacion de origen.

_SHOPIFY_ORIGENES = tuple(
    o.strip() for o in os.getenv(
        "SHOPIFY_FORM_ORIGINS",
        "https://www.welduapp.com,https://welduapp.com",
    ).split(",") if o.strip()
)


def _cors(resp):
    origen = request.headers.get("Origin", "")
    if origen in _SHOPIFY_ORIGENES:
        resp.headers["Access-Control-Allow-Origin"] = origen
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
        resp.headers["Vary"] = "Origin"
    return resp


# Campos que tienen columna propia en leads; el resto va a las notas.
_FORM_A_COLUMNA = {
    "empresa":   "empresa_nombre",
    "industria": "tipo_industria",
}
# Campos de plomeria que no aportan nada al vendedor dentro de las notas.
_FORM_IGNORAR = {
    "nombre", "name", "email", "correo", "telefono", "phone", "whatsapp",
    "mensaje", "body", "website", "_gotcha", "marca", "unidad",
    "payload_json", "fecha",
}


def _form_notas(data: dict, encabezado: str) -> str:
    """Vuelca en las notas todo lo que el formulario pregunto y el CRM no
    tiene donde guardar.

    Se hace generico a proposito: el cuestionario de aromas trae 25 campos y
    solo 5 tienen columna. Si mañana agregan una pregunta, aparece sola en
    las notas en vez de perderse en silencio, que es lo que pasaba antes.
    """
    lineas = [encabezado]
    mensaje = (data.get("mensaje") or data.get("body") or "").strip()
    if mensaje:
        lineas.append(f"Mensaje: {mensaje[:1000]}")

    # El resultado del diagnostico va arriba: es lo que el vendedor necesita
    # en los primeros cinco segundos. Lo demas es el porque, y va despues.
    # Lo que no este en la lista se agrega al final, asi una pregunta nueva
    # aparece igual en vez de perderse.
    orden = ["arquetipo", "compatibilidad", "intensidad_recomendada",
             "intensidad_deseada", "aroma_1", "aroma_2", "aroma_3", "aroma_4",
             "aroma_5", "proveedor_actual_competencia", "usa_aromatizacion",
             "sucursales", "espacios", "emociones", "personalidad", "objetivo",
             "permanencia", "clientes"]
    claves = sorted(data.keys(),
                    key=lambda c: (orden.index(str(c).strip().lower())
                                   if str(c).strip().lower() in orden else len(orden)))

    extras = []
    for clave in claves:
        valor = data[clave]
        k = str(clave).strip().lower()
        if k in _FORM_IGNORAR or k in _FORM_A_COLUMNA:
            continue
        texto = str(valor).strip() if valor is not None else ""
        if not texto or texto.lower() in ("none", "null", "[]", "{}"):
            continue
        etiqueta = k.replace("_", " ").capitalize()
        extras.append(f"  {etiqueta}: {texto[:300]}")
    if extras:
        lineas.append("")
        lineas.append("Respuestas del formulario:")
        lineas.extend(extras)
    return "\n".join(lineas)


@webhooks_bp.route("/form", methods=["OPTIONS"])
@webhooks_bp.route("/shopify/form", methods=["OPTIONS"])
def form_preflight():
    return _cors(jsonify({"ok": True}))


@webhooks_bp.route("/form", methods=["POST"])
@webhooks_bp.route("/shopify/form", methods=["POST"])
@limiter.limit("10 per hour")
def shopify_form():
    data = request.get_json(silent=True) or request.form.to_dict() or {}

    # Campo trampa: invisible para una persona, irresistible para un bot.
    if (data.get("website") or data.get("_gotcha") or "").strip():
        logger.info("[form] descartado por campo trampa")
        return _cors(jsonify({"status": "ok"}))

    datos = {
        "nombre":   (data.get("nombre") or data.get("name") or "").strip(),
        "email":    (data.get("email") or data.get("correo") or "").strip().lower(),
        "telefono": (data.get("telefono") or data.get("whatsapp")
                     or data.get("phone") or "").strip(),
    }

    # La unidad la dice el formulario. Sin esto, el cuestionario de aromas
    # —que es de Aromatex— entraria como Weldex y lo recibirian los
    # vendedores equivocados. Se valida contra las UN reales para que un
    # valor cualquiera no se cuele como marca.
    from un_filter import normalizar_un
    marca = normalizar_un(data.get("marca") or data.get("unidad") or "") or SHOPIFY_MARCA

    extra = {"marca_interes": marca}
    for campo, columna in _FORM_A_COLUMNA.items():
        valor = (data.get(campo) or "").strip() if data.get(campo) else ""
        if valor:
            extra[columna] = valor[:190]

    nota = _form_notas(data, f"Formulario web ({marca}).")
    lead = _shopify_alta_lead(datos, extra, nota)
    if not lead:
        # Se responde ok igual: el visitante no tiene por que enterarse de
        # como filtramos, y un error le haria reintentar.
        return _cors(jsonify({"status": "ok"}))
    return _cors(jsonify({"status": "ok", "lead_id": str(lead.id)}))

# blueprints/tickets.py
"""
Portal público de tickets — sin login, por enlace único por cliente.
El cliente reporta una incidencia y aterriza como CSIncidencia normal,
visible en /cs/incidencias y en la vista de la cuenta.

/soporte/<token>         → Centro de Incidencias (dashboard: historial + status)
/soporte/<token>/nuevo   → Levantar un ticket nuevo
"""
import json
import os
import re
import uuid
from datetime import date, timedelta
from flask import Blueprint, render_template, request, jsonify, current_app, url_for
from werkzeug.utils import secure_filename
from extensions import db, limiter
from models import CSAccount, CSIncidencia, CSPropiedad

tickets_bp = Blueprint("tickets", __name__)

# Límites de columna (models.py) — se validan aquí para no depender de que
# Postgres rechace el INSERT y tumbe la petición con un 500 sin manejar.
_MAXLEN = {
    "servicio": 30,
    "tipo": 100,
    "zona": 100,
    "quien_reporta": 200,
    "contacto_cliente": 200,
    "propiedad_nombre": 300,
}

_FOTOS_BUCKET = "ticket-evidencia"
_FOTOS_MAX_ARCHIVOS = 5
_FOTOS_MAX_BYTES = 8 * 1024 * 1024  # 8MB por foto

_SLA_HORAS = 24

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
FROM_EMAIL = "Grupo Avantex <crm@grupoavantex.com>"


def _es_email_valido(valor: str) -> bool:
    return bool(_EMAIL_RE.match(valor or ""))


def _get_storage():
    """Cliente de Supabase Storage, o None si no está configurado.
    Degrada con gracia: sin fotos no debe tumbar el registro del ticket."""
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_KEY")
    if not url or not key:
        return None
    from supabase import create_client
    return create_client(url, key).storage


def _asegura_bucket(storage):
    try:
        storage.get_bucket(_FOTOS_BUCKET)
    except Exception:
        try:
            storage.create_bucket(_FOTOS_BUCKET, options={
                "public": True,
                "file_size_limit": _FOTOS_MAX_BYTES,
                "allowed_mime_types": ["image/jpeg", "image/png", "image/webp", "image/heic"],
            })
        except Exception as e:
            current_app.logger.warning("No se pudo crear/verificar bucket %s: %s", _FOTOS_BUCKET, e)


def _subir_fotos(account_id, files) -> list[str]:
    """Sube hasta _FOTOS_MAX_ARCHIVOS imágenes a Supabase Storage.
    Cualquier archivo que falle se omite sin tumbar el resto del ticket."""
    storage = _get_storage()
    if not storage or not files:
        return []
    _asegura_bucket(storage)
    bucket = storage.from_(_FOTOS_BUCKET)

    urls = []
    for f in files[:_FOTOS_MAX_ARCHIVOS]:
        if not f or not f.filename:
            continue
        if not (f.mimetype or "").startswith("image/"):
            continue
        data = f.read()
        if not data or len(data) > _FOTOS_MAX_BYTES:
            continue
        path = f"tickets/{account_id}/{uuid.uuid4().hex}_{secure_filename(f.filename)}"
        try:
            bucket.upload(path, data, file_options={"content-type": f.mimetype})
            urls.append(bucket.get_public_url(path))
        except Exception as e:
            current_app.logger.warning("Fallo al subir foto de ticket: %s", e)
    return urls


def _evidencia_urls(inc) -> list[str]:
    try:
        return json.loads(inc.evidencia) if inc.evidencia else []
    except (ValueError, TypeError):
        return []


def _vencido(inc) -> bool:
    return (
        inc.status != "Resuelta"
        and inc.fecha_compromiso is not None
        and inc.fecha_compromiso < date.today()
    )


def _enviar_confirmacion_email(account, inc):
    """Correo de confirmación con folio + SLA — degrada con gracia si Resend
    no está configurado o el envío falla (nunca debe tumbar el ticket)."""
    if not RESEND_API_KEY or not inc.contacto_cliente:
        return
    try:
        import resend
        resend.api_key = RESEND_API_KEY

        kam = account.kam
        kam_nombre = kam.nombre if kam else "nuestro equipo de Customer Success"
        centro_url = url_for("tickets.centro_tickets", token=account.ticket_token, _external=True)
        compromiso = inc.fecha_compromiso.strftime("%d/%m/%Y") if inc.fecha_compromiso else None

        html = f"""<div style="font-family:Arial,sans-serif;color:#333;max-width:560px;margin:0 auto;">
            <h2 style="color:#4c1d95;margin-bottom:4px;">Ticket {inc.folio}</h2>
            <p style="color:#888;margin-top:0;">{account.nombre}</p>
            <p>Hola {inc.quien_reporta or ''},</p>
            <p>Recibimos tu reporte y ya quedó registrado con el folio <strong>{inc.folio}</strong>.</p>
            <table style="width:100%;border-collapse:collapse;margin:16px 0;font-size:13px;">
                <tr><td style="padding:4px 0;color:#888;width:120px;">Servicio</td><td>{inc.servicio}</td></tr>
                <tr><td style="padding:4px 0;color:#888;">Tipo</td><td>{inc.tipo}</td></tr>
                {f'<tr><td style="padding:4px 0;color:#888;">Ubicación</td><td>{inc.propiedad_nombre}</td></tr>' if inc.propiedad_nombre else ''}
                <tr><td style="padding:4px 0;color:#888;">Detalle</td><td>{inc.detalle or '—'}</td></tr>
            </table>
            <p><strong>{kam_nombre}</strong> te dará seguimiento en menos de {_SLA_HORAS} horas{f' (antes del {compromiso})' if compromiso else ''}.</p>
            <p><a href="{centro_url}" style="display:inline-block;background:#7c3aed;color:#fff;padding:10px 18px;border-radius:8px;text-decoration:none;font-weight:600;">Ver mis tickets</a></p>
            <p style="color:#aaa;font-size:12px;margin-top:24px;">Grupo Avantex &copy; 2026</p>
        </div>"""

        resend.Emails.send({
            "from": FROM_EMAIL,
            "to": [inc.contacto_cliente],
            "reply_to": kam.correo if kam else None,
            "subject": f"Ticket {inc.folio} recibido — {account.nombre}",
            "html": html,
        })
    except Exception as e:
        current_app.logger.warning("No se pudo enviar correo de confirmación de ticket %s: %s", inc.folio, e)


@tickets_bp.route("/<token>")
def centro_tickets(token):
    """Centro de Incidencias del cliente: historial + status de sus tickets."""
    account = CSAccount.query.filter_by(ticket_token=token).first()
    if not account:
        return render_template("tickets/not_found.html"), 404

    tickets = (
        CSIncidencia.query.filter_by(account_id=account.id)
        .order_by(CSIncidencia.created_at.desc())
        .limit(50).all()
    )
    for t in tickets:
        t.evidencia_urls = _evidencia_urls(t)
        t.vencido = _vencido(t)

    stats = {
        "abiertas": sum(1 for t in tickets if t.status == "Abierta"),
        "en_proceso": sum(1 for t in tickets if t.status == "En proceso"),
        "resueltas": sum(1 for t in tickets if t.status == "Resuelta"),
    }
    kam_nombre = account.kam.nombre if account.kam else None

    return render_template(
        "tickets/centro.html", account=account, token=token,
        tickets=tickets, stats=stats, kam_nombre=kam_nombre,
    )


@tickets_bp.route("/<token>/nuevo")
def nuevo_ticket_form(token):
    account = CSAccount.query.filter_by(ticket_token=token).first()
    if not account:
        return render_template("tickets/not_found.html"), 404
    return render_template("tickets/form.html", account=account, token=token)


@tickets_bp.route("/<token>/propiedades")
def propiedades_publico(token):
    """Búsqueda de sucursales de la cuenta, para el buscador del formulario
    público. Equivalente a cs.api_propiedades pero autorizado por token en
    vez de sesión — no expone nada fuera de la cuenta dueña del link."""
    account = CSAccount.query.filter_by(ticket_token=token).first()
    if not account:
        return jsonify([]), 404
    q = request.args.get("q", "").strip()
    query = CSPropiedad.query.filter_by(account_id=account.id)
    if q:
        query = query.filter(CSPropiedad.nombre.ilike(f"%{q}%"))
    props = query.order_by(CSPropiedad.nombre).limit(50).all()
    return jsonify([{
        "id": str(p.id), "nombre": p.nombre,
        "zona": p.zona, "unidad_negocio": p.unidad_negocio,
    } for p in props])


@tickets_bp.route("/<token>/nuevo", methods=["POST"])
@limiter.limit("5 per hour")
def enviar_ticket(token):
    account = CSAccount.query.filter_by(ticket_token=token).first()
    if not account:
        return render_template("tickets/not_found.html"), 404

    def _rerender(error):
        return render_template(
            "tickets/form.html", account=account, token=token, error=error,
        ), 400

    servicio = (request.form.get("servicio") or "").strip()
    tipo = (request.form.get("tipo") or "").strip()
    detalle = (request.form.get("detalle") or "").strip()
    quien_reporta = (request.form.get("quien_reporta") or "").strip()
    contacto_cliente = (request.form.get("contacto_cliente") or "").strip()
    ubicacion = (request.form.get("ubicacion") or "").strip()
    propiedad_id = (request.form.get("propiedad_id") or "").strip() or None

    if not tipo or not quien_reporta or not contacto_cliente:
        return _rerender("Faltan campos requeridos: tipo de incidencia, nombre y correo.")

    if not _es_email_valido(contacto_cliente):
        return _rerender("El correo no parece válido — revisa que esté bien escrito.")

    # Si viene una propiedad_id, debe pertenecer a esta misma cuenta — igual
    # que valida blueprints/cs.py::crear_incidencia para el flujo interno.
    if propiedad_id:
        prop = db.session.get(CSPropiedad, propiedad_id)
        if not prop or str(prop.account_id) != str(account.id):
            propiedad_id = None
        else:
            ubicacion = prop.nombre

    for campo, valor in (
        ("servicio", servicio), ("tipo", tipo), ("quien_reporta", quien_reporta),
        ("contacto_cliente", contacto_cliente), ("propiedad_nombre", ubicacion),
    ):
        if len(valor) > _MAXLEN[campo]:
            return _rerender(f"El campo '{campo}' es demasiado largo (máx {_MAXLEN[campo]} caracteres).")

    fotos = _subir_fotos(account.id, request.files.getlist("fotos"))
    hoy = date.today()

    inc = CSIncidencia(
        account_id=account.id,
        propiedad_id=propiedad_id,
        propiedad_nombre=ubicacion,
        servicio=servicio or "Aroma",
        tipo=tipo,
        detalle=detalle,
        status="Abierta",
        quien_reporta=quien_reporta,
        contacto_cliente=contacto_cliente,
        fecha_incidencia=hoy,
        # SLA de atención: 24h desde que se reporta. Alimenta directamente el
        # concepto de "vencida" ya usado en /cs/incidencias y en las alertas.
        fecha_compromiso=hoy + timedelta(hours=_SLA_HORAS),
        evidencia=json.dumps(fotos) if fotos else "",
        created_by="Cliente (portal)",
    )
    db.session.add(inc)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return _rerender("No se pudo registrar el ticket. Intenta de nuevo o contacta a tu KAM.")

    _enviar_confirmacion_email(account, inc)

    return render_template(
        "tickets/gracias.html", account=account, folio=inc.folio, token=token,
        email_enviado=bool(RESEND_API_KEY),
    )

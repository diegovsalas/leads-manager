# blueprints/tickets.py
"""
Portal público de tickets — sin login, por enlace único por cliente.
El cliente reporta una incidencia y aterriza como CSIncidencia normal,
visible en /cs/incidencias y en la vista de la cuenta.
"""
import json
import os
import uuid
from datetime import date
from flask import Blueprint, render_template, request, jsonify, current_app
from werkzeug.utils import secure_filename
from extensions import db, limiter
from models import CSAccount, CSIncidencia, CSPropiedad
from cs_alerts import resumen_tickets_mes

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


@tickets_bp.route("/<token>")
def ticket_publico(token):
    account = CSAccount.query.filter_by(ticket_token=token).first()
    if not account:
        return render_template("tickets/not_found.html"), 404
    resumen = resumen_tickets_mes(account.id)
    return render_template("tickets/form.html", account=account, token=token, resumen=resumen)


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


@tickets_bp.route("/<token>", methods=["POST"])
@limiter.limit("5 per hour")
def enviar_ticket(token):
    account = CSAccount.query.filter_by(ticket_token=token).first()
    if not account:
        return render_template("tickets/not_found.html"), 404

    def _rerender(error):
        return render_template(
            "tickets/form.html", account=account, token=token,
            resumen=resumen_tickets_mes(account.id), error=error,
        ), 400

    servicio = (request.form.get("servicio") or "").strip()
    tipo = (request.form.get("tipo") or "").strip()
    detalle = (request.form.get("detalle") or "").strip()
    quien_reporta = (request.form.get("quien_reporta") or "").strip()
    contacto_cliente = (request.form.get("contacto_cliente") or "").strip()
    ubicacion = (request.form.get("ubicacion") or "").strip()
    propiedad_id = (request.form.get("propiedad_id") or "").strip() or None

    if not tipo or not quien_reporta or not contacto_cliente:
        return _rerender("Faltan campos requeridos: tipo de incidencia, nombre y contacto.")

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
        fecha_incidencia=date.today(),
        evidencia=json.dumps(fotos) if fotos else "",
        created_by="Cliente (portal)",
    )
    db.session.add(inc)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return _rerender("No se pudo registrar el ticket. Intenta de nuevo o contacta a tu KAM.")

    return render_template(
        "tickets/gracias.html", account=account, folio=inc.folio,
        resumen=resumen_tickets_mes(account.id),
    )

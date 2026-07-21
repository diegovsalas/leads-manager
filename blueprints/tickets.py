# blueprints/tickets.py
"""
Portal público de tickets — sin login, por enlace único por cliente.
El cliente reporta una incidencia y aterriza como CSIncidencia normal,
visible en /cs/incidencias y en la vista de la cuenta.
"""
from datetime import date
from flask import Blueprint, render_template, request
from extensions import db, limiter
from models import CSAccount, CSIncidencia

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


@tickets_bp.route("/<token>")
def ticket_publico(token):
    account = CSAccount.query.filter_by(ticket_token=token).first()
    if not account:
        return render_template("tickets/not_found.html"), 404
    return render_template("tickets/form.html", account=account, token=token)


@tickets_bp.route("/<token>", methods=["POST"])
@limiter.limit("5 per hour")
def enviar_ticket(token):
    account = CSAccount.query.filter_by(ticket_token=token).first()
    if not account:
        return render_template("tickets/not_found.html"), 404

    servicio = (request.form.get("servicio") or "").strip()
    tipo = (request.form.get("tipo") or "").strip()
    detalle = (request.form.get("detalle") or "").strip()
    quien_reporta = (request.form.get("quien_reporta") or "").strip()
    contacto_cliente = (request.form.get("contacto_cliente") or "").strip()
    ubicacion = (request.form.get("ubicacion") or "").strip()

    if not tipo or not quien_reporta or not contacto_cliente:
        return render_template(
            "tickets/form.html", account=account, token=token,
            error="Faltan campos requeridos: tipo de incidencia, nombre y contacto.",
        ), 400

    for campo, valor in (
        ("servicio", servicio), ("tipo", tipo), ("quien_reporta", quien_reporta),
        ("contacto_cliente", contacto_cliente), ("propiedad_nombre", ubicacion),
    ):
        if len(valor) > _MAXLEN[campo]:
            return render_template(
                "tickets/form.html", account=account, token=token,
                error=f"El campo '{campo}' es demasiado largo (máx {_MAXLEN[campo]} caracteres).",
            ), 400

    inc = CSIncidencia(
        account_id=account.id,
        propiedad_nombre=ubicacion,
        servicio=servicio or "Aroma",
        tipo=tipo,
        detalle=detalle,
        status="Abierta",
        quien_reporta=quien_reporta,
        contacto_cliente=contacto_cliente,
        fecha_incidencia=date.today(),
        created_by="Cliente (portal)",
    )
    db.session.add(inc)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return render_template(
            "tickets/form.html", account=account, token=token,
            error="No se pudo registrar el ticket. Intenta de nuevo o contacta a tu KAM.",
        ), 400

    return render_template("tickets/gracias.html", account=account)

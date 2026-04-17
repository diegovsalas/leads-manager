# blueprints/encuesta.py
"""
Encuesta pública NPS + CSAT (7 dimensiones) — sin login requerido.
"""
from flask import Blueprint, render_template, request, redirect
from extensions import db
from models import CSAccount, CSEncuesta

encuesta_bp = Blueprint("encuesta", __name__)


@encuesta_bp.route("/<token>")
def encuesta_publica(token):
    account = CSAccount.query.filter_by(survey_token=token).first()
    if not account:
        return render_template("encuesta/not_found.html"), 404
    return render_template("encuesta/form.html", account=account, token=token)


@encuesta_bp.route("/<token>/enviar", methods=["POST"])
def enviar_encuesta(token):
    account = CSAccount.query.filter_by(survey_token=token).first()
    if not account:
        return "No encontrado", 404

    nps = request.form.get("nps")
    csat = request.form.get("csat")
    if nps is None or csat is None:
        return redirect(f"/encuesta/{token}")

    def _int(val):
        try:
            return int(val)
        except (TypeError, ValueError):
            return None

    respuesta = CSEncuesta(
        account_id=account.id,
        token=token,
        nombre_respondente=request.form.get("nombre", "").strip(),
        puesto_respondente=request.form.get("puesto", "").strip(),
        nps=_int(nps),
        csat=_int(csat),
        csat_calidad=_int(request.form.get("csat_calidad")),
        csat_respuesta=_int(request.form.get("csat_respuesta")),
        csat_comunicacion=_int(request.form.get("csat_comunicacion")),
        csat_precio=_int(request.form.get("csat_precio")),
        csat_tecnico=_int(request.form.get("csat_tecnico")),
        comentario=request.form.get("comentario", "").strip(),
    )
    db.session.add(respuesta)

    # Recalcular NPS promedio de la cuenta
    from sqlalchemy import func
    avg_nps = db.session.query(func.avg(CSEncuesta.nps)).filter_by(account_id=account.id).scalar()
    if avg_nps is not None:
        account.nps = round(float(avg_nps), 1)

    db.session.commit()
    return render_template("encuesta/gracias.html", account=account)

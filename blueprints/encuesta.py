# blueprints/encuesta.py
"""
Encuesta pública NPS + CSAT (7 dimensiones) — sin login requerido.
"""
import os
from html import escape

from flask import Blueprint, render_template, request, redirect, current_app, url_for
from extensions import db
from models import CSAccount, CSEncuesta

RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
FROM_EMAIL = "Grupo Avantex <crm@grupoavantex.com>"
# FEAT-2026-10-05: copia de cada respuesta, además del KAM de la cuenta.
# Se puede cambiar sin deploy con ENCUESTA_NOTIF_CC (separados por coma).
NOTIF_CC = [c.strip() for c in os.getenv(
    "ENCUESTA_NOTIF_CC",
    "diegovelazquez@grupoavantex.com,jessicasantin@grupoavantex.com").split(",") if c.strip()]

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
        version=2,
        csat=_int(csat),
        csat_cumplimiento=_int(request.form.get("csat_cumplimiento")),
        csat_respuesta=_int(request.form.get("csat_respuesta")),
        csat_comunicacion=_int(request.form.get("csat_comunicacion")),
        csat_gestion_kam=_int(request.form.get("csat_gestion_kam")),
        csat_confianza_kam=_int(request.form.get("csat_confianza_kam")),
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
    _notificar_respuesta(account, respuesta)
    return render_template("encuesta/gracias.html", account=account)


def _notificar_respuesta(account, r):
    """Avisa al KAM (con copia a CS) que el cliente contestó.

    Nunca debe tumbar la encuesta: la respuesta ya está guardada, y un
    cliente no tiene por qué ver un error porque Resend falló.
    """
    if not RESEND_API_KEY or r.nps is None:
        return
    kam = account.kam
    para = [kam.correo] if kam and kam.correo else []
    cc = [c for c in NOTIF_CC if c not in para]
    if not para and not cc:
        return
    try:
        import resend
        resend.api_key = RESEND_API_KEY

        cat = ("Promotor", "#1D7A4C") if r.nps >= 9 else \
              ("Pasivo", "#B7791F") if r.nps >= 7 else ("Detractor", "#C2412F")
        dims = [("Satisfacción general", r.csat), ("Cumplimiento", r.csat_cumplimiento),
                ("Tiempo de respuesta", r.csat_respuesta), ("Comunicación KAM", r.csat_comunicacion),
                ("Gestión KAM", r.csat_gestion_kam), ("Confianza KAM", r.csat_confianza_kam),
                ("Calidad-precio", r.csat_precio), ("Equipo técnico", r.csat_tecnico)]
        filas = "".join(
            f'<tr><td style="padding:3px 0;color:#5B6D8C;">{et}</td>'
            f'<td style="padding:3px 0;text-align:right;">{"★" * v}{"☆" * (5 - v)}</td></tr>'
            for et, v in dims if v)
        ficha = url_for("cs.account_detail", account_id=account.id, _external=True)
        urgente = r.nps <= 6

        html = f"""<div style="font-family:Arial,sans-serif;color:#0C1F3E;max-width:560px;margin:0 auto;">
            <div style="background:#0C1F3E;color:#fff;padding:18px 22px;border-bottom:4px solid #0954E6;">
                <div style="font-size:12px;letter-spacing:1px;color:#A9BDE4;">ENCUESTA NPS / CSAT</div>
                <div style="font-size:19px;font-weight:bold;margin-top:4px;">{escape(account.nombre)}</div>
            </div>
            <div style="padding:20px 22px;border:1px solid #DBE4F0;border-top:none;">
                {'<p style="background:#FBE9E7;color:#C2412F;padding:10px 12px;border-radius:6px;font-weight:bold;margin-top:0;">Detractor: contactar al cliente lo antes posible.</p>' if urgente else ''}
                <p style="margin-top:0;"><strong>{escape(r.nombre_respondente or 'El cliente')}</strong>{' · ' + escape(r.puesto_respondente) if r.puesto_respondente else ''} respondió la encuesta.</p>
                <p style="font-size:15px;">NPS <span style="display:inline-block;background:{cat[1]};color:#fff;font-weight:bold;padding:3px 10px;border-radius:5px;">{r.nps}</span>
                   <span style="color:{cat[1]};font-weight:bold;margin-left:6px;">{cat[0]}</span>
                   &nbsp;·&nbsp; CSAT {r.csat_promedio if r.csat_promedio is not None else '—'} / 5</p>
                <table style="width:100%;border-collapse:collapse;font-size:13px;margin:10px 0 14px;">{filas}</table>
                <p style="font-size:13px;color:#5B6D8C;margin-bottom:4px;">Comentario</p>
                <p style="background:#F4F7FB;padding:10px 12px;border-radius:6px;margin-top:0;white-space:pre-line;">{escape(r.comentario) if r.comentario else '<em style="color:#637594;">Sin comentario</em>'}</p>
                <p><a href="{ficha}" style="display:inline-block;background:#0954E6;color:#fff;padding:10px 18px;border-radius:8px;text-decoration:none;font-weight:bold;">Ver ficha del cliente</a></p>
                <p style="color:#637594;font-size:12px;margin-bottom:0;">KAM: {escape(kam.nombre) if kam else 'sin KAM asignado'}</p>
            </div>
        </div>"""

        resend.Emails.send({
            "from": FROM_EMAIL,
            "to": para or cc,
            "cc": cc if para else [],
            "subject": f"{'⚠️ Detractor — ' if urgente else ''}NPS {r.nps} · {account.nombre} respondió la encuesta",
            "html": html,
        })
    except Exception as e:
        current_app.logger.warning("No se pudo avisar la respuesta de encuesta de %s: %s",
                                   account.nombre, e)

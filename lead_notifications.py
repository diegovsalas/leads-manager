"""Avisos de asignación privados, transaccionales y con reintentos persistentes."""
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from html import escape
from zoneinfo import ZoneInfo

import requests
from flask import current_app, has_app_context
from sqlalchemy import event, inspect, select, update
from sqlalchemy.orm import Session, object_session

from extensions import db
from models import Lead, LeadAssignmentNotice, UserCRM

_INSTALLED = False
FROM_EMAIL = "CRM Avantex <crm@grupoavantex.com>"


def _capture(mapper, connection, lead):
    history = inspect(lead).attrs.usuario_asignado_id.history
    if not lead.usuario_asignado_id or not history.has_changes():
        return
    old = history.deleted[0] if history.deleted else None
    if str(old) == str(lead.usuario_asignado_id):
        return
    now = datetime.now(timezone.utc)
    connection.execute(LeadAssignmentNotice.__table__.insert().values(
        id=uuid.uuid4(), lead_id=lead.id, vendedor_id=lead.usuario_asignado_id,
        created_at=now, next_attempt_at=now, attempts=0, status="pendiente",
        snapshot={"nombre": lead.nombre or "Lead", "empresa": lead.empresa_nombre or "",
                  "marca": lead.marca_interes or "Sin marca",
                  "tipo": "reasignado" if old else "asignado"},
    ))
    object_session(lead).info["lead_notices_pending"] = True


def _after_commit(session):
    if session.info.pop("lead_notices_pending", False) and has_app_context():
        schedule_dispatch(current_app._get_current_object())


def _after_rollback(session):
    session.info.pop("lead_notices_pending", None)


def _payload(notice, email):
    data = notice["snapshot"]
    date = notice["created_at"].astimezone(ZoneInfo("America/Monterrey")).strftime("%d/%m/%Y %H:%M")
    link = os.getenv("DOMAIN", "https://leads-manager-avantex.onrender.com").rstrip("/") + "/?lead=" + str(notice["lead_id"])
    subject = "Lead " + data["tipo"] + " · " + data["marca"]
    html = f'''<div style="font-family:Arial,sans-serif;max-width:580px;margin:auto;color:#243047">
      <h2>Se te ha {escape(data['tipo'])} un lead</h2>
      <p><strong>Lead:</strong> {escape(data['nombre'])}</p>
      <p><strong>Empresa:</strong> {escape(data['empresa'] or 'Sin empresa registrada')}</p>
      <p><strong>Marca:</strong> {escape(data['marca'])}</p>
      <p><strong>Fecha de asignación:</strong> {date} (Monterrey)</p>
      <p><a href="{escape(link, quote=True)}" style="display:inline-block;background:#0954e6;color:white;padding:12px 18px;border-radius:6px;text-decoration:none">Abrir lead en el CRM</a></p>
      <p>Consulta los datos de contacto y da seguimiento dentro del CRM.</p></div>'''
    text = f"Lead {data['tipo']}\nLead: {data['nombre']}\nEmpresa: {data['empresa']}\nMarca: {data['marca']}\nFecha de asignación: {date} (Monterrey)\nAbrir lead en el CRM: {link}"
    return {"from": FROM_EMAIL, "to": [email], "subject": subject, "html": html, "text": text}


def _prepare():
    """Reserva un aviso y persiste su contenido antes de transmitirlo."""
    table = LeadAssignmentNotice.__table__
    now = datetime.now(timezone.utc)
    with db.engine.begin() as connection:
        notice = connection.execute(select(table).where(
            table.c.status == "pendiente", table.c.next_attempt_at <= now
        ).order_by(table.c.created_at).limit(1).with_for_update(skip_locked=True)).mappings().first()
        if not notice:
            return None
        owner = connection.execute(select(Lead.usuario_asignado_id).where(Lead.id == notice["lead_id"])).scalar()
        newer = connection.execute(select(table.c.id).where(
            table.c.lead_id == notice["lead_id"], table.c.created_at > notice["created_at"]
        ).limit(1)).first()
        if owner != notice["vendedor_id"] or newer:
            connection.execute(update(table).where(table.c.id == notice["id"]).values(status="omitida", last_error="asignacion_superada"))
            return {"skip": True}
        # Resend conserva las claves durante 24h. No arriesgar un duplicado
        # si el proceso estuvo detenido más allá de esa ventana.
        if notice["first_attempt_at"] and now - notice["first_attempt_at"] >= timedelta(hours=23):
            connection.execute(update(table).where(table.c.id == notice["id"]).values(status="revision", last_error="ventana_idempotencia_agotada"))
            return {"skip": True}
        payload = notice["payload"]
        if not payload:
            recipients = connection.execute(select(UserCRM.correo).where(
                UserCRM.usuario_id == owner, UserCRM.activo.is_(True)
            )).scalars().all()
            recipients = {e.strip().lower() for e in recipients if e and "@" in e}
            if len(recipients) != 1:
                connection.execute(update(table).where(table.c.id == notice["id"]).values(
                    next_attempt_at=now + timedelta(minutes=30), last_error="correo_ausente_o_ambiguo"))
                return {"skip": True}
            payload = _payload(notice, recipients.pop())
        else:
            # Un aviso pendiente no debe ir a una cuenta que fue desactivada.
            active = connection.execute(select(UserCRM.id).where(
                UserCRM.usuario_id == owner, UserCRM.activo.is_(True),
                db.func.lower(UserCRM.correo) == payload["to"][0]
            )).first()
            if not active:
                connection.execute(update(table).where(table.c.id == notice["id"]).values(status="omitida", last_error="destinatario_inactivo"))
                return {"skip": True}
        attempts = notice["attempts"] + 1
        connection.execute(update(table).where(table.c.id == notice["id"]).values(
            payload=payload, attempts=attempts,
            first_attempt_at=notice["first_attempt_at"] or now,
            next_attempt_at=now + timedelta(minutes=2), last_error=None))
        return {"id": notice["id"], "attempts": attempts, "payload": payload}


def dispatch_pending(limit=20):
    key = os.getenv("RESEND_API_KEY")
    if not key:
        return {"enviadas": 0, "motivo": "RESEND_API_KEY no configurada"}
    sent = 0
    for _ in range(limit):
        notice = _prepare()
        if notice is None:
            break
        if notice.get("skip"):
            continue
        provider_id = None
        error = None
        try:
            response = requests.post("https://api.resend.com/emails", timeout=(5, 15),
                headers={"Authorization": "Bearer " + key,
                         "Idempotency-Key": "lead-assignment/" + str(notice["id"])},
                json=notice["payload"])
            if response.ok:
                provider_id = response.json().get("id")
                if not provider_id:
                    error = "respuesta_sin_id"
            else:
                error = "resend_http_" + str(response.status_code)
        except (requests.RequestException, ValueError):
            error = "envio_no_confirmado"
        now = datetime.now(timezone.utc)
        values = {"last_error": error}
        if provider_id:
            values.update(status="enviada", sent_at=now, provider_id=provider_id)
            sent += 1
        else:
            values["next_attempt_at"] = now + timedelta(minutes=min(30, 2 ** min(notice["attempts"], 5)))
        table = LeadAssignmentNotice.__table__
        with db.engine.begin() as connection:
            connection.execute(update(table).where(
                table.c.id == notice["id"], table.c.attempts == notice["attempts"]
            ).values(**values))
        if error:
            current_app.logger.warning("Aviso de asignación %s pendiente: %s", notice["id"], error)
    return {"enviadas": sent}


def schedule_dispatch(app, periodic=False):
    if not app.config.get("LEAD_ASSIGNMENT_EMAIL_DISPATCH", True) or not os.getenv("RESEND_API_KEY"):
        return
    state = app.extensions.setdefault("lead_notice_dispatch", {"busy": False, "last": 0})
    now = time.monotonic()
    if state["busy"] or (periodic and now - state["last"] < 60):
        return
    state.update(busy=True, last=now)

    def run():
        try:
            with app.app_context():
                dispatch_pending()
        except Exception:
            app.logger.exception("No se pudo procesar la cola de avisos de asignación")
        finally:
            state["busy"] = False
    from gevent import spawn
    spawn(run)


def init_app(app):
    global _INSTALLED
    if not _INSTALLED:
        # Cargar el valor anterior también si el atributo estaba expirado.
        event.listen(Lead.usuario_asignado_id, "set", lambda *args: None, active_history=True)
        event.listen(Lead, "after_insert", _capture)
        event.listen(Lead, "after_update", _capture)
        event.listen(Session, "after_commit", _after_commit)
        event.listen(Session, "after_rollback", _after_rollback)
        _INSTALLED = True

    @app.after_request
    def retry_notices(response):
        schedule_dispatch(app, periodic=True)
        return response

# actividad.py
"""
Helper para registrar actividad en el log de auditoría.
Uso:
    from actividad import log_actividad
    log_actividad("crear", "lead", lead.id, "Lead creado: Juan Perez")
"""
from flask import session
from extensions import db
from models import ActividadLog


def log_actividad(accion, entidad, entidad_id=None, detalle=None):
    """
    Registra una actividad en el log.

    accion: crear, editar, mover, eliminar, cotizar, enviar, login, etc.
    entidad: lead, cotizacion, vendedor, meta, gasto, etc.
    entidad_id: UUID de la entidad afectada
    detalle: texto descriptivo libre
    """
    try:
        log = ActividadLog(
            usuario_nombre=session.get("user_nombre", "Sistema"),
            usuario_id=session.get("user_id"),
            accion=accion,
            entidad=entidad,
            entidad_id=entidad_id,
            detalle=detalle,
        )
        db.session.add(log)
        db.session.commit()
    except Exception:
        db.session.rollback()

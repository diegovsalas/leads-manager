# blueprints/cs.py
"""
CS Dashboard — Customer Success para KAMs.
Rutas bajo /cs/
"""
import csv
import hashlib
import io
from datetime import datetime, date, timedelta
from functools import wraps
from flask import Blueprint, render_template, request, redirect, url_for, session, jsonify, send_file, flash
from sqlalchemy import func
from extensions import db
from blueprints.auth import is_admin_role, is_full_access_role
from models import (
    CSAccount, CSInvoice, CSAppointment, CSNote, CSTask,
    CSOnboardingAccount, CSOpportunity, CSContacto, CSEntregable,
    CSEncuesta, CSIncidencia, CSPropiedad, CSWorkloadSurvey,
    UserCRM, RolCRM,
)
from cs_health_score import calcular_health_score, calcular_health_scores_batch
from cs_alerts import generar_alertas, alertas_por_cuenta

cs_bp = Blueprint("cs", __name__, template_folder="../templates/cs")

ETAPAS_PIPELINE = [
    ("prospeccion", "Prospección"),
    ("propuesta_enviada", "Propuesta Enviada"),
    ("negociacion", "Negociación"),
    ("ganada", "Ganada"),
    ("perdida", "Perdida"),
]

TIPOS_OPORTUNIDAD = [
    ("upsell_un", "Upsell de UN"),
    ("expansion_sucursales", "Expansión de sucursales"),
    ("nuevo_servicio", "Nuevo servicio"),
]

WORKLOAD_ACTIVITIES = [
    "Descarga de evidencias de servicio",
    "Carga de evidencias en plataformas",
    "Solicitud de permisos de acceso",
    "Seguimiento de permisos",
    "Seguimiento de citas y programación",
    "Atención de incidencias",
    "Coordinación de incidencias con Operaciones",
    "Solicitud y seguimiento de órdenes de compra",
    "Comunicación con técnicos",
    "Comunicación con clientes",
    "Elaboración de reportes",
    "Reuniones internas",
    "Seguimiento a facturación y cobranza",
    "Planeación y coordinación de servicios",
]

WORKLOAD_HOUR_OPTIONS = ["No aplica", "1-2 h", "3-5 h", "6-10 h", "11-15 h", "16-20 h", "Más de 20 h"]
WORKLOAD_ACTIVITY_HOUR_OPTIONS = ["No aplica", "1-2 h", "3-5 h", "6-10 h", "11-15 h", "Más de 15 h"]
WORKLOAD_COUNT_OPTIONS = ["0", "1-2", "3-5", "6-10", "11-20", "Más de 20"]
WORKLOAD_SURVEY_OPTIONS = {
    "hours": WORKLOAD_HOUR_OPTIONS,
    "activity_hours": WORKLOAD_ACTIVITY_HOUR_OPTIONS,
    "counts": WORKLOAD_COUNT_OPTIONS,
    "carga_esperada": [
        "No aplica / cliente sin actividad este mes",
        "No",
        "Sí, ligeramente mayor",
        "Sí, mucho mayor",
    ],
    "motivo_carga": [
        "Seguimiento normal",
        "Evidencias / reportes",
        "Plataformas externas",
        "Permisos / accesos",
        "Citas / programación",
        "Incidencias",
        "Coordinación con operaciones",
        "Comunicación con cliente",
        "Comunicación con técnicos",
        "Órdenes de compra",
        "Facturación / cobranza",
        "Reuniones",
        "Implementación / onboarding",
        "Reprogramaciones",
        "Otro operativo",
    ],
    "entregables_count": [
        "No",
        "Sí, 1 entregable recurrente",
        "Sí, 2-3 entregables recurrentes",
        "Sí, 4-5 entregables recurrentes",
        "Sí, más de 5 entregables recurrentes",
    ],
    "entregables_tipos": [
        "Evidencias de servicio",
        "Reporte mensual",
        "Reporte semanal",
        "Certificados",
        "Minutas",
        "KPIs",
        "Órdenes de compra",
        "Facturación especial",
        "Acceso / permisos",
        "Seguimiento de incidencias",
        "Calendario de servicios",
        "Reunión de seguimiento",
        "Otro operativo",
    ],
    "frecuencia": [
        "No aplica",
        "Diario",
        "Semanal",
        "Quincenal",
        "Mensual",
        "Bimestral",
        "Trimestral",
        "Bajo demanda",
        "Cada servicio realizado",
    ],
    "dependencia": [
        "No",
        "Sí, del cliente",
        "Sí, de operaciones",
        "Sí, de técnicos",
        "Sí, de administración / facturación",
        "Sí, de una plataforma externa",
        "Sí, de varios equipos",
    ],
    "bloqueos_nivel": ["No", "Sí, pocos", "Sí, varios", "Sí, muchos", "Sí, críticos"],
    "tipo_bloqueo": [
        "No aplica",
        "Falta de respuesta del cliente",
        "Falta de respuesta de operaciones",
        "Evidencias incompletas",
        "Información incorrecta",
        "Técnicos sin información completa",
        "Reprogramaciones",
        "Permisos / accesos",
        "Plataformas externas",
        "Órdenes de compra",
        "Facturación / cobranza",
        "Incidencias recurrentes",
        "Falta de claridad en responsables",
        "Otro operativo",
    ],
    "recurrencia": ["No", "Sí, ocasionalmente", "Sí, frecuentemente", "Sí, todos los meses", "No sé"],
    "causa_incidencias": [
        "No aplica",
        "Servicio no realizado",
        "Servicio incompleto",
        "Mala comunicación con técnico",
        "Mala comunicación con cliente",
        "Problemas de acceso",
        "Problemas de calidad",
        "Falta de evidencia",
        "Reprogramación por cliente",
        "Reprogramación por operación",
        "Otro operativo",
    ],
}


def _get_kams():
    return UserCRM.query.filter_by(rol=RolCRM.KAM, activo=True).order_by(UserCRM.nombre).all()


def _get_assignables():
    """Usuarios que pueden ser asignados como kam_id de una cuenta CS.
    Incluye KAMs + Super Admins (admins pueden auto-asignarse cuentas).
    No incluye Vendedores (esos manejan leads, no CS accounts)."""
    return UserCRM.query.filter(
        UserCRM.rol.in_([RolCRM.KAM, RolCRM.SUPER_ADMIN]),
        UserCRM.activo.is_(True),
    ).order_by(UserCRM.rol.desc(), UserCRM.nombre).all()


def _is_kam():
    return session.get("user_rol", "").upper() == "KAM"


def _current_kam_id():
    if _is_kam():
        return session.get("user_id")
    return None


def _current_user_id():
    """ID del usuario logueado (cualquier rol)."""
    return session.get("user_id")


def _is_cs_admin():
    return is_admin_role()


def _is_cs_full_access():
    return is_full_access_role()


def _wants_json():
    return request.path.startswith("/api/") or request.accept_mimetypes.best == "application/json"


def _permission_denied(message="Sin permisos", status=403, json_response=None):
    if json_response is None:
        json_response = _wants_json()
    if json_response:
        return jsonify({"error": message}), status
    return message, status


def _not_found(message="Cuenta no encontrada", status=404, json_response=None):
    if json_response is None:
        json_response = _wants_json()
    if json_response:
        return jsonify({"error": message}), status
    return message, status


def _get_cs_account(account_id):
    try:
        return db.session.get(CSAccount, account_id)
    except (TypeError, ValueError):
        return None


def _can_edit_account(account):
    """Admins CS editan todo; KAM solo sus propias cuentas."""
    if not account:
        return False
    if _is_cs_admin():
        return True
    if _is_kam():
        return str(account.kam_id) == str(_current_kam_id())
    return False


def _require_cs_admin(json_response=None):
    if not _is_cs_admin():
        return _permission_denied("Solo admin CS", json_response=json_response)
    return None


def _require_cs_full_access(json_response=None):
    if not _is_cs_full_access():
        return _permission_denied("Solo acceso total CS", json_response=json_response)
    return None


def _require_account_access(account, json_response=None):
    if not account:
        return _not_found(json_response=json_response)
    if not _can_edit_account(account):
        return _permission_denied(json_response=json_response)
    return None


def require_cs_admin(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        err = _require_cs_admin()
        if err:
            return err
        return f(*args, **kwargs)
    return wrapper


def require_cs_full_access(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        err = _require_cs_full_access()
        if err:
            return err
        return f(*args, **kwargs)
    return wrapper


def require_cs_account_access(f):
    """Protege rutas con parámetro account_id."""
    @wraps(f)
    def wrapper(account_id, *args, **kwargs):
        account = _get_cs_account(account_id)
        err = _require_account_access(account)
        if err:
            return err
        return f(account_id, *args, **kwargs)
    return wrapper


def _can_edit_opportunity(opp):
    if not opp:
        return False
    if _is_cs_admin():
        return True
    if not _is_kam():
        return False
    if opp.account_id:
        account = _get_cs_account(opp.account_id)
        return _can_edit_account(account)
    return str(opp.kam_id) == str(_current_kam_id())


def _require_opportunity_access(opp):
    if not opp:
        return _not_found("Oportunidad no encontrada")
    if not _can_edit_opportunity(opp):
        return _permission_denied()
    return None


def _parse_adjuntos(form):
    """Extrae adjuntos del formulario y auto-detecta tipo por URL."""
    adjuntos = []
    for i in range(10):
        url = form.get(f"adj_url_{i}", "").strip()
        nombre = form.get(f"adj_nombre_{i}", "").strip()
        if not url:
            continue
        url_lower = url.lower()
        if "drive.google.com/drive/folders" in url_lower:
            tipo = "folder"
        elif "docs.google.com/spreadsheets" in url_lower or "sheets" in url_lower:
            tipo = "sheet"
        elif "docs.google.com/document" in url_lower:
            tipo = "doc"
        elif "docs.google.com/presentation" in url_lower:
            tipo = "slides"
        elif url_lower.endswith(".pdf"):
            tipo = "pdf"
        else:
            tipo = "link"
        adjuntos.append({"nombre": nombre or url[:40], "url": url, "tipo": tipo})
    return adjuntos


def _ctx():
    """Context vars comunes para todos los templates."""
    return {
        "user_nombre": session.get("user_nombre", ""),
        "user_rol": session.get("user_rol", ""),
        "is_kam": _is_kam(),
    }


def _generate_client_id():
    """Genera el siguiente client_id secuencial (AX-0001, AX-0002, ...)."""
    result = db.session.execute(
        db.text("SELECT MAX(client_id) FROM cs_accounts WHERE client_id IS NOT NULL")
    ).scalar()
    if result:
        num = int(result.split("-")[1]) + 1
    else:
        num = 1
    return f"AX-{num:04d}"


def _get_periodo():
    """
    Retorna (inicio, fin, label, periodo_param) según ?periodo= en query string.
    Formatos: '2026-Q1', '2026-04', 'all'. Default: mes actual.
    """
    param = request.args.get("periodo", "")

    if param and "-Q" in param:
        # Trimestre: 2026-Q1
        year = int(param.split("-Q")[0])
        quarter = int(param.split("-Q")[1])
        month_start = (quarter - 1) * 3 + 1
        inicio = date(year, month_start, 1)
        if month_start + 3 > 12:
            fin = date(year + 1, 1, 1)
        else:
            fin = date(year, month_start + 3, 1)
        label = f"Q{quarter} {year}"
    elif param and len(param) == 7:
        # Mes: 2026-04
        year, month = int(param[:4]), int(param[5:7])
        inicio = date(year, month, 1)
        if month == 12:
            fin = date(year + 1, 1, 1)
        else:
            fin = date(year, month + 1, 1)
        meses = ["", "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
                 "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"]
        label = f"{meses[month]} {year}"
    elif param == "all":
        inicio = date(2020, 1, 1)
        fin = date(2030, 1, 1)
        label = "Todo el historial"
    else:
        # Default: mes actual
        hoy = date.today()
        inicio = hoy.replace(day=1)
        if hoy.month == 12:
            fin = date(hoy.year + 1, 1, 1)
        else:
            fin = date(hoy.year, hoy.month + 1, 1)
        meses = ["", "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
                 "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"]
        label = f"{meses[hoy.month]} {hoy.year}"
        param = hoy.strftime("%Y-%m")

    return inicio, fin, label, param


def _periodos_disponibles():
    """Retorna lista de periodos para el selector."""
    return [
        {"value": "2026-Q1", "label": "Q1 2026 (Ene-Mar)"},
        {"value": "2026-Q2", "label": "Q2 2026 (Abr-Jun)"},
        {"value": "2026-01", "label": "Enero 2026"},
        {"value": "2026-02", "label": "Febrero 2026"},
        {"value": "2026-03", "label": "Marzo 2026"},
        {"value": "2026-04", "label": "Abril 2026"},
        {"value": "2026-05", "label": "Mayo 2026"},
        {"value": "2026-06", "label": "Junio 2026"},
        {"value": "all", "label": "Todo el historial"},
    ]


def _calc_sucursales_efectivas(account_ids):
    """FIX-2026-06-26: cuenta sucursales reales de cada cuenta.

    El campo cs_accounts.sucursales es manual y se queda en 0 si el KAM
    nunca lo edita. Esto deformaba los KPIs del dashboard (ej. Batia
    aparecía en 0 sucursales pese a tener 86 sucursales operando en citas).

    Estrategia (Opción 1 aprobada por Diego):
      sucursales_efectivas = MAX(manual, props_cargadas, citas_distintas)
      si los 3 son 0 → usa COUNT(DISTINCT concepto) de facturas como fallback.

    Nunca BAJA un número ya conocido. No persiste cambios — solo cómputo.

    Retorna dict {account_id (UUID): int}.
    """
    if not account_ids:
        return {}

    manual_rows = db.session.query(CSAccount.id, CSAccount.sucursales).filter(
        CSAccount.id.in_(account_ids)
    ).all()
    manual = {r[0]: (r[1] or 0) for r in manual_rows}

    props_rows = (
        db.session.query(CSPropiedad.account_id, func.count(CSPropiedad.id))
        .filter(CSPropiedad.account_id.in_(account_ids))
        .group_by(CSPropiedad.account_id).all()
    )
    props = {r[0]: r[1] for r in props_rows}

    citas_rows = (
        db.session.query(
            CSAppointment.account_id,
            func.count(func.distinct(CSAppointment.propiedad)),
        )
        .filter(
            CSAppointment.account_id.in_(account_ids),
            CSAppointment.propiedad.isnot(None),
            CSAppointment.propiedad != "",
        )
        .group_by(CSAppointment.account_id).all()
    )
    citas = {r[0]: r[1] for r in citas_rows}

    conc_rows = (
        db.session.query(
            CSInvoice.account_id,
            func.count(func.distinct(CSInvoice.concepto)),
        )
        .filter(
            CSInvoice.account_id.in_(account_ids),
            CSInvoice.concepto.isnot(None),
            func.trim(CSInvoice.concepto) != "",
        )
        .group_by(CSInvoice.account_id).all()
    )
    conceptos = {r[0]: r[1] for r in conc_rows}

    out = {}
    for aid in account_ids:
        m = manual.get(aid, 0)
        p = props.get(aid, 0)
        c = citas.get(aid, 0)
        max3 = max(m, p, c)
        out[aid] = max3 if max3 > 0 else conceptos.get(aid, 0)
    return out


def _calc_facturacion_periodo(account_ids, inicio, fin):
    """Calcula facturación del periodo. Separa pendiente vencido vs por cobrar (en plazo de crédito)."""
    from sqlalchemy import case
    hoy = date.today()
    rows = (
        db.session.query(
            CSInvoice.account_id,
            func.coalesce(func.sum(CSInvoice.total), 0),
            func.coalesce(func.sum(CSInvoice.pagado), 0),
            func.coalesce(func.sum(CSInvoice.pendiente), 0),
            # Vencido: pendiente de facturas cuya fecha_vencimiento ya pasó
            func.coalesce(func.sum(case(
                (db.and_(CSInvoice.pendiente > 0, CSInvoice.fecha_vencimiento < hoy), CSInvoice.pendiente),
                else_=0,
            )), 0),
            # Por cobrar: pendiente de facturas aún en plazo
            func.coalesce(func.sum(case(
                (db.and_(CSInvoice.pendiente > 0, db.or_(CSInvoice.fecha_vencimiento >= hoy, CSInvoice.fecha_vencimiento.is_(None))), CSInvoice.pendiente),
                else_=0,
            )), 0),
            func.count(CSInvoice.id),
        )
        .filter(
            CSInvoice.account_id.in_(account_ids),
            CSInvoice.fecha_cobro >= inicio,
            CSInvoice.fecha_cobro < fin,
        )
        .group_by(CSInvoice.account_id)
        .all()
    )
    result = {}
    for acc_id, total, pagado, pendiente, vencido, por_cobrar, num in rows:
        result[str(acc_id)] = {
            "facturado": float(total), "pagado": float(pagado),
            "pendiente": float(pendiente),
            "vencido": float(vencido),
            "por_cobrar": float(por_cobrar),
            "num_facturas": num,
        }
    return result


def _mrr_operativo(account):
    """MRR usado para capacidad de cartera: observado si existe, contratado si no."""
    return float(getattr(account, "mrr_observado", 0) or account.mrr or 0)


def _score_email_response(avg_hours):
    if avg_hours is None:
        return None
    if avg_hours <= 2:
        return 100
    if avg_hours <= 8:
        return 85
    if avg_hours <= 24:
        return 65
    if avg_hours <= 48:
        return 35
    return 10


def _weighted_score(parts):
    usable = [(score, weight) for score, weight in parts if score is not None and weight > 0]
    if not usable:
        return None
    return round(sum(score * weight for score, weight in usable) / sum(weight for _, weight in usable), 1)


def _score_status(score):
    if score is None:
        return {"label": "Sin datos", "tone": "neutral"}
    if score >= 80:
        return {"label": "Excelente", "tone": "ok"}
    if score >= 65:
        return {"label": "Bien", "tone": "info"}
    if score >= 45:
        return {"label": "Atención", "tone": "warn"}
    return {"label": "Riesgo", "tone": "danger"}


def _workload_hours_midpoint(value):
    if not value or value == "No aplica":
        return 0.0
    if value.startswith("Más de"):
        digits = "".join(ch for ch in value if ch.isdigit())
        return float(digits or 0)
    if "-" in value:
        nums = []
        for part in value.replace("h", "").split("-"):
            part = part.strip()
            if part.isdigit():
                nums.append(float(part))
        if len(nums) == 2:
            return sum(nums) / 2
    digits = "".join(ch for ch in value if ch.isdigit())
    return float(digits or 0)


def _workload_count_midpoint(value):
    if not value:
        return 0.0
    if value.startswith("Más de"):
        digits = "".join(ch for ch in value if ch.isdigit())
        return float(digits or 0)
    if "-" in value:
        nums = [float(part.strip()) for part in value.split("-") if part.strip().isdigit()]
        if len(nums) == 2:
            return sum(nums) / 2
    return float(value) if value.isdigit() else 0.0


def _build_carga_operativa(periodo_param):
    from collections import Counter, defaultdict

    surveys = (
        db.session.query(CSWorkloadSurvey, CSAccount, UserCRM)
        .join(CSAccount, CSAccount.id == CSWorkloadSurvey.account_id)
        .outerjoin(UserCRM, UserCRM.id == CSWorkloadSurvey.kam_id)
        .filter(CSWorkloadSurvey.periodo == periodo_param)
        .order_by(CSAccount.nombre)
        .all()
    )

    accounts_q = CSAccount.query.filter(db.or_(CSAccount.en_due_diligence.is_(None), CSAccount.en_due_diligence.is_(False)))
    if _is_kam():
        accounts_q = accounts_q.filter_by(kam_id=_current_kam_id())
        surveys = [row for row in surveys if str(row[0].kam_id) == str(_current_kam_id())]
    accounts = accounts_q.all()
    account_ids = [a.id for a in accounts]
    scores_map = calcular_health_scores_batch(accounts)

    account_by_id = {str(a.id): a for a in accounts}
    survey_account_ids = {str(s.account_id) for s, _, _ in surveys}

    activity_counter = Counter()
    blocker_counter = Counter()
    deliverable_counter = Counter()
    client_rows = []
    kam_rows = {}
    alerts = []

    kams = _get_kams()
    if _is_kam():
        current_kam_id = str(_current_kam_id())
        kams = [kam for kam in kams if str(kam.id) == current_kam_id]
    for kam in kams:
        kam_key = str(kam.id)
        kam_accounts = [a for a in accounts if str(a.kam_id) == kam_key]
        kam_rows[kam_key] = {
            "kam": kam,
            "kam_name": kam.nombre,
            "assigned": len(kam_accounts),
            "answered": 0,
            "hours": 0.0,
            "top_client": None,
            "top_activity": "",
            "top_blocker": "",
            "activities": Counter(),
            "blockers": Counter(),
        }

    for survey, account, kam in surveys:
        if str(account.id) not in account_by_id:
            continue
        kam_key = str(kam.id) if kam else "sin_kam"
        kam_name = kam.nombre if kam else "Sin KAM"
        if kam_key not in kam_rows:
            kam_accounts = [a for a in accounts if str(a.kam_id) == kam_key]
            kam_rows[kam_key] = {
                "kam": kam,
                "kam_name": kam_name,
                "assigned": len(kam_accounts),
                "answered": 0,
                "hours": 0.0,
                "top_client": None,
                "top_activity": "",
                "top_blocker": "",
                "activities": Counter(),
                "blockers": Counter(),
            }

        client_hours = _workload_hours_midpoint(survey.horas_cliente)
        activity_hours = 0.0
        for item in survey.actividades_horas or []:
            if not isinstance(item, dict):
                continue
            name = item.get("actividad") or "Sin actividad"
            hours = _workload_hours_midpoint(item.get("horas"))
            if hours > 0:
                activity_counter[name] += hours
                kam_rows[kam_key]["activities"][name] += hours
                activity_hours += hours

        blocker_hours = _workload_hours_midpoint(survey.horas_bloqueos)
        if survey.tipo_bloqueo and survey.tipo_bloqueo != "No aplica" and blocker_hours > 0:
            blocker_counter[survey.tipo_bloqueo] += blocker_hours
            kam_rows[kam_key]["blockers"][survey.tipo_bloqueo] += blocker_hours

        for entregable in survey.entregables_tipos or []:
            deliverable_counter[entregable] += 1

        health = scores_map.get(str(account.id), {"score": None, "categoria": "Sin datos"})
        mrr = _mrr_operativo(account)
        client_row = {
            "account": account,
            "kam_name": kam_name,
            "hours": client_hours,
            "activity_hours": activity_hours,
            "mrr": mrr,
            "health": health,
            "motivo": survey.motivo_carga or "Sin dato",
            "bloqueo": survey.tipo_bloqueo or "Sin dato",
            "entregables": len(survey.entregables_tipos or []),
            "dependencia": survey.dependencia_externa or "Sin dato",
            "url": url_for("cs.account_detail", account_id=account.id) + f"?periodo={periodo_param}&tab=workload-survey",
        }
        client_rows.append(client_row)

        kam_rows[kam_key]["answered"] += 1
        kam_rows[kam_key]["hours"] += client_hours
        if not kam_rows[kam_key]["top_client"] or client_hours > kam_rows[kam_key]["top_client"]["hours"]:
            kam_rows[kam_key]["top_client"] = {"name": account.nombre, "hours": client_hours}

        if client_hours >= 16 and mrr < 50000:
            alerts.append({
                "tone": "warn",
                "title": "Carga alta con bajo MRR",
                "detail": f"{account.nombre} reporta {client_hours:.0f}h y MRR ${mrr:,.0f}.",
                "url": client_row["url"],
            })
        if client_hours >= 16 and health.get("categoria") in ("Riesgo", "Atención"):
            alerts.append({
                "tone": "danger" if health.get("categoria") == "Riesgo" else "warn",
                "title": "Carga alta con salud comprometida",
                "detail": f"{account.nombre} está en {health.get('categoria')} y consume {client_hours:.0f}h.",
                "url": client_row["url"],
            })
        if blocker_hours >= 6 or survey.recurrencia_bloqueo in ("Sí, frecuentemente", "Sí, todos los meses"):
            alerts.append({
                "tone": "info",
                "title": "Bloqueo recurrente",
                "detail": f"{account.nombre}: {survey.tipo_bloqueo or 'bloqueo'} ({survey.recurrencia_bloqueo or 'sin recurrencia'}).",
                "url": client_row["url"],
            })

    for row in kam_rows.values():
        row["progress"] = round(row["answered"] / row["assigned"] * 100, 1) if row["assigned"] else 0
        row["top_activity"] = row["activities"].most_common(1)[0][0] if row["activities"] else "Sin dato"
        row["top_blocker"] = row["blockers"].most_common(1)[0][0] if row["blockers"] else "Sin dato"

    answered_ids = survey_account_ids & set(account_by_id)
    pending = max(len(account_ids) - len(answered_ids), 0)
    totals = {
        "surveys": len(client_rows),
        "accounts": len(account_ids),
        "pending": pending,
        "hours": round(sum(r["hours"] for r in client_rows), 1),
        "top_activity": activity_counter.most_common(1)[0][0] if activity_counter else "Sin datos",
        "top_blocker": blocker_counter.most_common(1)[0][0] if blocker_counter else "Sin datos",
    }

    return {
        "totals": totals,
        "kam_rows": sorted(kam_rows.values(), key=lambda r: (-r["hours"], r["kam_name"])),
        "client_rows": sorted(client_rows, key=lambda r: r["hours"], reverse=True)[:20],
        "activity_rows": [{"name": k, "hours": round(v, 1)} for k, v in activity_counter.most_common(12)],
        "blocker_rows": [{"name": k, "hours": round(v, 1)} for k, v in blocker_counter.most_common(12)],
        "deliverable_rows": [{"name": k, "count": v} for k, v in deliverable_counter.most_common(12)],
        "alerts": alerts[:10],
    }


def _kam_success_plays(row):
    """Playbook accionable a partir de brechas visibles en el scorecard."""
    plays = []
    kam_url = f"/cs/kam/{row['kam'].id}"

    def add(title, detail, priority, tone, action_label="Abrir cartera", url=None):
        plays.append({
            "title": title,
            "detail": detail,
            "priority": priority,
            "tone": tone,
            "action_label": action_label,
            "url": url or kam_url,
        })

    if row["risk_without_plan"] > 0:
        add(
            "Plan de rescate",
            f"{row['risk_without_plan']} cuenta(s) en riesgo no tienen tarea abierta de seguimiento.",
            1,
            "danger",
        )
    if row["overdue_tasks"] > 0:
        add(
            "Backlog vencido",
            f"Cerrar o reprogramar {row['overdue_tasks']} tarea(s) vencidas antes del siguiente corte.",
            2,
            "warn",
            "Ver pendientes",
            "/cs/mis-pendientes",
        )
    if row["no_touch_30"] > 0:
        add(
            "Reactivar contacto",
            f"{row['no_touch_30']} cuenta(s) no registran contacto en los ultimos 30 dias.",
            3,
            "warn",
        )
    if row["avg_response_hours"] is not None and row["avg_response_hours"] > 24:
        add(
            "SLA de respuesta",
            f"Respuesta promedio de {row['avg_response_hours']}h; objetivo recomendado menor a 24h.",
            4,
            "warn",
        )
    if row["incidents_open"] > 0:
        add(
            "Cierre operativo",
            f"{row['incidents_open']} incidencia(s) abiertas requieren empuje con operaciones.",
            5,
            "info",
        )
    if row["qbr_period"] == 0 and row["accounts"] >= 3:
        add(
            "QBR pendiente",
            "No hay QBRs creados en el periodo para una cartera activa.",
            6,
            "info",
        )
    if not plays and row["accounts"] > 0:
        add(
            "Mantener cadencia",
            "La cartera no muestra brechas criticas; mantener revisiones y actualizar notas.",
            9,
            "ok",
        )

    return sorted(plays, key=lambda p: p["priority"])[:3]


def _build_kam_scorecard(inicio, fin):
    """Scorecard de ejecución KAM.

    El health score mide la salud del cliente. Este score mide gestión:
    cobertura de contacto, control de tareas, planes para cuentas en riesgo,
    respuesta por email y seguimiento operativo.
    """
    from models import KAMEmailResponse

    inicio_dt = datetime.combine(inicio, datetime.min.time())
    fin_dt = datetime.combine(fin, datetime.min.time())
    last_30_dt = datetime.now() - timedelta(days=30)
    today = date.today()

    kams = _get_kams()
    if _is_kam():
        current_id = str(_current_kam_id())
        kams = [k for k in kams if str(k.id) == current_id]

    accounts_query = CSAccount.query.filter(
        db.or_(CSAccount.en_due_diligence.is_(None), CSAccount.en_due_diligence.is_(False))
    )
    if _is_kam():
        accounts_query = accounts_query.filter_by(kam_id=_current_kam_id())
    accounts = accounts_query.all()
    scores_map = calcular_health_scores_batch(accounts)
    accounts_by_kam = {}
    for account in accounts:
        accounts_by_kam.setdefault(str(account.kam_id), []).append(account)

    rows = []
    totals = {
        "kams": len(kams),
        "accounts": 0,
        "mrr": 0,
        "risk_accounts": 0,
        "overdue_tasks": 0,
        "open_tasks": 0,
        "risk_without_plan": 0,
        "score_avg": None,
    }

    for kam in kams:
        kam_accounts = accounts_by_kam.get(str(kam.id), [])
        account_ids = [a.id for a in kam_accounts]
        account_id_set = {str(a.id) for a in kam_accounts}
        account_count = len(kam_accounts)

        valid_scores = []
        risk_accounts = []
        attention_accounts = []
        for account in kam_accounts:
            health = scores_map.get(str(account.id), {})
            score = health.get("score")
            if score is not None:
                valid_scores.append(score)
            if health.get("categoria") == "Riesgo":
                risk_accounts.append(account)
            elif health.get("categoria") == "Atención":
                attention_accounts.append(account)

        health_avg = round(sum(valid_scores) / len(valid_scores), 1) if valid_scores else None

        if account_ids:
            open_tasks = CSTask.query.filter(
                CSTask.account_id.in_(account_ids),
                CSTask.completada.is_(False),
            ).all()
            overdue_tasks = [t for t in open_tasks if t.fecha_limite and t.fecha_limite < today]
            tasks_created_period = CSTask.query.filter(
                CSTask.account_id.in_(account_ids),
                CSTask.created_at >= inicio_dt,
                CSTask.created_at < fin_dt,
            ).count()
            tasks_completed_period = CSTask.query.filter(
                CSTask.account_id.in_(account_ids),
                CSTask.created_at >= inicio_dt,
                CSTask.created_at < fin_dt,
                CSTask.completada.is_(True),
            ).count()
            qbr_period = CSTask.query.filter(
                CSTask.account_id.in_(account_ids),
                CSTask.created_at >= inicio_dt,
                CSTask.created_at < fin_dt,
                CSTask.tipo.ilike("%qbr%"),
            ).count()

            notes_touch = {
                str(row[0]) for row in db.session.query(CSNote.account_id).filter(
                    CSNote.account_id.in_(account_ids),
                    CSNote.created_at >= inicio_dt,
                    CSNote.created_at < fin_dt,
                ).distinct().all()
            }
            appointments_touch = {
                str(row[0]) for row in db.session.query(CSAppointment.account_id).filter(
                    CSAppointment.account_id.in_(account_ids),
                    CSAppointment.fecha_inicio >= inicio_dt,
                    CSAppointment.fecha_inicio < fin_dt,
                ).distinct().all()
            }
            task_touch = {
                str(row[0]) for row in db.session.query(CSTask.account_id).filter(
                    CSTask.account_id.in_(account_ids),
                    CSTask.created_at >= inicio_dt,
                    CSTask.created_at < fin_dt,
                ).distinct().all()
            }
            email_touch = {
                str(row[0]) for row in db.session.query(KAMEmailResponse.account_id).filter(
                    KAMEmailResponse.kam_id == kam.id,
                    KAMEmailResponse.account_id.in_(account_ids),
                    KAMEmailResponse.replied_at >= inicio_dt,
                    KAMEmailResponse.replied_at < fin_dt,
                ).distinct().all() if row[0]
            }
            touched_accounts = notes_touch | appointments_touch | task_touch | email_touch

            recent_notes = {
                str(row[0]) for row in db.session.query(CSNote.account_id).filter(
                    CSNote.account_id.in_(account_ids),
                    CSNote.created_at >= last_30_dt,
                ).distinct().all()
            }
            recent_appointments = {
                str(row[0]) for row in db.session.query(CSAppointment.account_id).filter(
                    CSAppointment.account_id.in_(account_ids),
                    CSAppointment.fecha_inicio >= last_30_dt,
                ).distinct().all()
            }
            recent_emails = {
                str(row[0]) for row in db.session.query(KAMEmailResponse.account_id).filter(
                    KAMEmailResponse.kam_id == kam.id,
                    KAMEmailResponse.account_id.in_(account_ids),
                    KAMEmailResponse.replied_at >= last_30_dt,
                ).distinct().all() if row[0]
            }

            appointment_rows = CSAppointment.query.filter(
                CSAppointment.account_id.in_(account_ids),
                CSAppointment.fecha_inicio >= inicio_dt,
                CSAppointment.fecha_inicio < fin_dt,
            ).all()
            completed_appointments = [
                a for a in appointment_rows
                if (a.estatus or "").strip().lower() in ("terminada", "completada", "realizada")
            ]

            incidents_open = CSIncidencia.query.filter(
                CSIncidencia.account_id.in_(account_ids),
                db.or_(CSIncidencia.status.is_(None), ~CSIncidencia.status.ilike("%resuelta%")),
            ).count()

            email_stats = db.session.query(
                func.count(KAMEmailResponse.id),
                func.avg(KAMEmailResponse.response_hours),
            ).filter(
                KAMEmailResponse.kam_id == kam.id,
                KAMEmailResponse.replied_at >= inicio_dt,
                KAMEmailResponse.replied_at < fin_dt,
            ).first()
        else:
            open_tasks = []
            overdue_tasks = []
            tasks_created_period = 0
            tasks_completed_period = 0
            qbr_period = 0
            touched_accounts = set()
            recent_notes = set()
            recent_appointments = set()
            recent_emails = set()
            appointment_rows = []
            completed_appointments = []
            incidents_open = 0
            email_stats = (0, None)

        risk_ids = {str(a.id) for a in risk_accounts}
        risk_with_plan = {
            str(t.account_id) for t in open_tasks
            if str(t.account_id) in risk_ids
        }
        risk_without_plan = max(len(risk_ids) - len(risk_with_plan), 0)
        no_touch_30 = max(account_count - len((recent_notes | recent_appointments | recent_emails) & account_id_set), 0)

        touch_pct = round((len(touched_accounts) / account_count * 100), 1) if account_count else 0
        task_score = max(0, 100 - (len(overdue_tasks) * 12))
        touch_score = min(100, (touch_pct / 80) * 100) if account_count else None
        risk_plan_score = (len(risk_with_plan) / len(risk_ids) * 100) if risk_ids else 100
        avg_response_hours = float(email_stats[1]) if email_stats and email_stats[1] is not None else None
        email_score = _score_email_response(avg_response_hours)
        operation_score = (
            len(completed_appointments) / len(appointment_rows) * 100
            if appointment_rows else None
        )

        score = _weighted_score([
            (touch_score, 30),
            (task_score, 25),
            (risk_plan_score, 20),
            (email_score, 15),
            (operation_score, 10),
        ])

        row = {
            "kam": kam,
            "score": score,
            "status": _score_status(score),
            "accounts": account_count,
            "mrr": sum(_mrr_operativo(a) for a in kam_accounts),
            "health_avg": health_avg,
            "risk_accounts": len(risk_accounts),
            "attention_accounts": len(attention_accounts),
            "risk_with_plan": len(risk_with_plan),
            "risk_without_plan": risk_without_plan,
            "open_tasks": len(open_tasks),
            "overdue_tasks": len(overdue_tasks),
            "tasks_created": tasks_created_period,
            "tasks_completed": tasks_completed_period,
            "qbr_period": qbr_period,
            "touch_pct": touch_pct,
            "touched_accounts": len(touched_accounts),
            "no_touch_30": no_touch_30,
            "appointments": len(appointment_rows),
            "completed_appointments": len(completed_appointments),
            "operation_score": round(operation_score, 1) if operation_score is not None else None,
            "emails": int(email_stats[0] or 0) if email_stats else 0,
            "avg_response_hours": round(avg_response_hours, 1) if avg_response_hours is not None else None,
            "incidents_open": incidents_open,
        }
        row["plays"] = _kam_success_plays(row)
        rows.append(row)

        totals["accounts"] += account_count
        totals["mrr"] += row["mrr"]
        totals["risk_accounts"] += row["risk_accounts"]
        totals["risk_without_plan"] += risk_without_plan
        totals["overdue_tasks"] += len(overdue_tasks)
        totals["open_tasks"] += len(open_tasks)

    rows.sort(key=lambda r: (r["score"] is None, -(r["score"] or 0), r["kam"].nombre))
    scored = [r["score"] for r in rows if r["score"] is not None]
    totals["score_avg"] = round(sum(scored) / len(scored), 1) if scored else None
    all_plays = []
    for row in rows:
        for play in row["plays"]:
            if play["tone"] == "ok":
                continue
            item = dict(play)
            item["kam"] = row["kam"].nombre
            item["kam_id"] = row["kam"].id
            all_plays.append(item)
    totals["plays"] = sorted(all_plays, key=lambda p: p["priority"])[:6]
    return rows, totals


# ══════════════════════════════════════════════
# DASHBOARD
# ══════════════════════════════════════════════
@cs_bp.route("/")
def dashboard():
    inicio, fin, periodo_label, periodo_param = _get_periodo()

    kam_filter = _current_kam_id()
    q = CSAccount.query
    if kam_filter:
        q = q.filter_by(kam_id=kam_filter)
    # FEAT-2026-07-06: excluir cuentas en Due Diligence de los KPIs generales
    q = q.filter(db.or_(CSAccount.en_due_diligence.is_(None),
                        CSAccount.en_due_diligence.is_(False)))
    # FEAT-2026-06-29: filtro global por UN
    from un_filter import filtrar_cs_accounts_por_un
    q = filtrar_cs_accounts_por_un(q, CSAccount, request.args.get("un"))
    accounts = q.all()
    account_ids = [a.id for a in accounts]

    # Facturación dinámica del periodo
    fact_periodo = _calc_facturacion_periodo(account_ids, inicio, fin)

    scores_map = calcular_health_scores_batch(accounts)

    mrr_total = sum(float(a.mrr or 0) for a in accounts)
    mrr_observado_total = sum(float(getattr(a, "mrr_observado", 0) or 0) for a in accounts)
    arr_total = sum(float(a.arr_proyectado or 0) for a in accounts)
    arr_observado_total = mrr_observado_total * 12
    # FIX-2026-06-26: usar sucursales efectivas (max de fuentes), no el
    # campo manual que se queda en 0 cuando el KAM no lo edita.
    suc_efectivas = _calc_sucursales_efectivas(account_ids)
    total_sucursales = sum(suc_efectivas.values())
    # Gap % (qué tan desactualizado está Savio respecto al recurrente real)
    gap_pct = ((mrr_observado_total - mrr_total) / mrr_total * 100) if mrr_total > 0 else 0

    # Facturación del periodo
    facturado_periodo = sum(f["facturado"] for f in fact_periodo.values())
    pagado_periodo = sum(f["pagado"] for f in fact_periodo.values())
    pendiente_periodo = sum(f["pendiente"] for f in fact_periodo.values())
    vencido_periodo = sum(f.get("vencido", 0) for f in fact_periodo.values())
    por_cobrar_periodo = sum(f.get("por_cobrar", 0) for f in fact_periodo.values())

    # Comparación con periodo anterior (solo si es mes individual)
    delta_facturado = delta_pagado = delta_pendiente = None
    if len(periodo_param) == 7 and "-Q" not in periodo_param and periodo_param != "all":
        # Calcular mes anterior
        y, m = int(periodo_param[:4]), int(periodo_param[5:7])
        if m == 1:
            prev_inicio = date(y - 1, 12, 1)
            prev_fin = date(y, 1, 1)
        else:
            prev_inicio = date(y, m - 1, 1)
            prev_fin = date(y, m, 1)
        fact_prev = _calc_facturacion_periodo(account_ids, prev_inicio, prev_fin)
        prev_facturado = sum(f["facturado"] for f in fact_prev.values())
        prev_pagado = sum(f["pagado"] for f in fact_prev.values())
        prev_pendiente = sum(f["pendiente"] for f in fact_prev.values())
        if prev_facturado > 0:
            delta_facturado = round((facturado_periodo - prev_facturado) / prev_facturado * 100, 1)
        if prev_pagado > 0:
            delta_pagado = round((pagado_periodo - prev_pagado) / prev_pagado * 100, 1)
        if prev_pendiente > 0:
            delta_pendiente = round((pendiente_periodo - prev_pendiente) / prev_pendiente * 100, 1)

    account_scores = []
    for acc in accounts:
        hs = scores_map[str(acc.id)]
        fp = fact_periodo.get(str(acc.id), {"facturado": 0, "pagado": 0, "pendiente": 0})
        account_scores.append({"account": acc, "health": hs, "fact": fp})
    # FIX-2026-07-03: score puede ser None (sin datos). Al ordenar por
    # riesgo, tratamos None como score alto (final de la lista, no lo
    # confundimos con riesgo real).
    account_scores.sort(key=lambda x: x["health"]["score"] if x["health"]["score"] is not None else 999)
    top_riesgo = [x for x in account_scores if x["health"]["score"] is not None][:5]

    cat_counts = {"Sana": 0, "Atención": 0, "Riesgo": 0, "Sin datos": 0}
    for item in account_scores:
        c = item["health"]["categoria"]
        cat_counts[c] = cat_counts.get(c, 0) + 1

    # ── KPIs avanzados ────────────────────────────────────────────────
    num_acc = max(len(accounts), 1)
    cuentas_con_mrr = [a for a in accounts if (a.mrr or 0) > 0]
    arpa = mrr_total / num_acc if num_acc else 0
    pct_cobranza = (pagado_periodo / facturado_periodo * 100) if facturado_periodo > 0 else 0
    pct_riesgo = (cat_counts["Riesgo"] / num_acc * 100) if num_acc else 0
    pct_atencion = (cat_counts["Atención"] / num_acc * 100) if num_acc else 0
    # FIX-2026-07-03: promediar SOLO cuentas con score válido.
    scores_validos = [item["health"]["score"] for item in account_scores if item["health"]["score"] is not None]
    hs_avg = round(sum(scores_validos) / len(scores_validos), 1) if scores_validos else 0

    # NPS promedio del portafolio (de los CSAccount.nps si está populado)
    nps_vals = [float(a.nps) for a in accounts if a.nps is not None]
    nps_avg = round(sum(nps_vals) / len(nps_vals), 1) if nps_vals else None

    # Concentración: top 5 cuentas por MRR OBSERVADO (la realidad operativa) / total
    top_mrr_sorted = sorted(accounts, key=lambda a: float(getattr(a, "mrr_observado", 0) or 0), reverse=True)[:5]
    top5_mrr = sum(float(getattr(a, "mrr_observado", 0) or 0) for a in top_mrr_sorted)
    top5_concentracion = (top5_mrr / mrr_observado_total * 100) if mrr_observado_total > 0 else 0
    top_cuentas_mrr = [
        {"account": a,
         "mrr": float(a.mrr or 0),
         "mrr_observado": float(getattr(a, "mrr_observado", 0) or 0),
         "pct_total": ((float(getattr(a, "mrr_observado", 0) or 0)) / mrr_observado_total * 100) if mrr_observado_total > 0 else 0,
         "health": scores_map[str(a.id)]}
        for a in top_mrr_sorted
    ]

    kams = _get_kams()
    kam_data = []
    for k in kams:
        accs_kam = [a for a in accounts if str(a.kam_id) == str(k.id)]
        kam_data.append({
            "id": str(k.id), "nombre": k.nombre,
            "num_cuentas": len(accs_kam),
            "mrr": sum(float(a.mrr or 0) for a in accs_kam),
            "mrr_observado": sum(float(getattr(a, "mrr_observado", 0) or 0) for a in accs_kam),
            # FIX-2026-06-26: usar sucursales efectivas (max de fuentes)
            "sucursales": sum(suc_efectivas.get(a.id, 0) for a in accs_kam),
        })

    cuentas_onboarding = [a for a in accounts if a.es_cuenta_nueva]
    pipeline = CSOnboardingAccount.query.all()
    alertas = generar_alertas(accounts=accounts, scores_map=scores_map)
    alertas_criticas = [a for a in alertas if a["severidad"] == "critica"]

    # Sucursales por UN (propiedades únicas por tipo de servicio)
    from sqlalchemy import case, distinct
    suc_un_rows = (
        db.session.query(
            func.count(distinct(case(
                (CSAppointment.titulo_servicio.ilike("%aroma%"), CSAppointment.propiedad),
                (CSAppointment.titulo_servicio.ilike("%instalacion%"), CSAppointment.propiedad),
            ))),
            func.count(distinct(case(
                (CSAppointment.titulo_servicio.ilike("%fumig%"), CSAppointment.propiedad),
                (CSAppointment.titulo_servicio.ilike("%plaga%"), CSAppointment.propiedad),
            ))),
        )
        .filter(CSAppointment.account_id.in_(account_ids))
        .first()
    )
    suc_aromatex = suc_un_rows[0] if suc_un_rows else 0
    suc_pestex = suc_un_rows[1] if suc_un_rows else 0

    return render_template(
        "cs_dashboard.html",
        mrr_total=mrr_total, arr_total=arr_total,
        mrr_observado_total=mrr_observado_total, arr_observado_total=arr_observado_total,
        gap_pct=gap_pct,
        num_cuentas=len(accounts), total_sucursales=total_sucursales,
        suc_aromatex=suc_aromatex, suc_pestex=suc_pestex,
        facturado_periodo=facturado_periodo, pagado_periodo=pagado_periodo,
        pendiente_periodo=pendiente_periodo,
        vencido_periodo=vencido_periodo, por_cobrar_periodo=por_cobrar_periodo,
        delta_facturado=delta_facturado, delta_pagado=delta_pagado,
        delta_pendiente=delta_pendiente,
        top_riesgo=top_riesgo, cat_counts=cat_counts,
        # KPIs avanzados
        arpa=arpa, pct_cobranza=pct_cobranza,
        pct_riesgo=pct_riesgo, pct_atencion=pct_atencion,
        hs_avg=hs_avg, nps_avg=nps_avg,
        top5_concentracion=top5_concentracion,
        top_cuentas_mrr=top_cuentas_mrr,
        cuentas_con_mrr_n=len(cuentas_con_mrr),
        kam_data=kam_data, cuentas_onboarding=cuentas_onboarding,
        alertas=alertas, alertas_criticas=alertas_criticas,
        pipeline=pipeline, account_scores=account_scores,
        periodo_label=periodo_label, periodo_param=periodo_param,
        periodos=_periodos_disponibles(),
        **_ctx(),
    )


# ══════════════════════════════════════════════
# CLIENTES — directorio
# ══════════════════════════════════════════════
@cs_bp.route("/clientes")
def clientes():
    kam_filter = _current_kam_id()
    q = CSAccount.query
    if kam_filter:
        q = q.filter_by(kam_id=kam_filter)
    # FEAT-2026-07-06: excluir cuentas Due Diligence del directorio de clientes
    q = q.filter(db.or_(CSAccount.en_due_diligence.is_(None),
                        CSAccount.en_due_diligence.is_(False)))
    accounts = q.order_by(CSAccount.nombre).all()
    scores_map = calcular_health_scores_batch(accounts)

    # Sucursales por UN en batch
    from sqlalchemy import case as sa_case, distinct
    account_ids = [a.id for a in accounts]
    suc_un_rows = (
        db.session.query(
            CSAppointment.account_id,
            func.count(distinct(sa_case(
                (CSAppointment.titulo_servicio.ilike("%aroma%"), CSAppointment.propiedad),
                (CSAppointment.titulo_servicio.ilike("%instalacion%"), CSAppointment.propiedad),
            ))),
            func.count(distinct(sa_case(
                (CSAppointment.titulo_servicio.ilike("%fumig%"), CSAppointment.propiedad),
                (CSAppointment.titulo_servicio.ilike("%plaga%"), CSAppointment.propiedad),
            ))),
        )
        .filter(CSAppointment.account_id.in_(account_ids))
        .group_by(CSAppointment.account_id)
        .all()
    ) if account_ids else []
    suc_un_map = {str(r[0]): {"aromatex": r[1], "pestex": r[2]} for r in suc_un_rows}

    clientes_data = []
    for acc in accounts:
        hs = scores_map[str(acc.id)]
        owners = CSContacto.query.filter_by(account_id=acc.id, is_owner=True).all()
        suc = suc_un_map.get(str(acc.id), {"aromatex": 0, "pestex": 0})
        clientes_data.append({
            "account": acc, "health": hs,
            "owners": owners,
            "suc_aromatex": suc["aromatex"], "suc_pestex": suc["pestex"],
        })

    return render_template(
        "cs_clientes.html",
        clientes=clientes_data, kams=_get_assignables(),
        **_ctx(),
    )


# ══════════════════════════════════════════════
# MIS CUENTAS — vista personalizada del usuario logueado (cualquier rol)
# ══════════════════════════════════════════════
@cs_bp.route("/mis-cuentas")
def mis_cuentas():
    """Lista de cuentas CS donde kam_id == usuario actual. Independiente del
    rol — funciona para KAM, admin, super_admin."""
    user_id = _current_user_id()
    if not user_id:
        return redirect(url_for("auth.login_page"))

    accounts = CSAccount.query.filter_by(kam_id=user_id).order_by(CSAccount.mrr.desc()).all()
    scores_map = calcular_health_scores_batch(accounts) if accounts else {}

    # Tasks pendientes por cuenta
    account_data = []
    for acc in accounts:
        hs = scores_map.get(str(acc.id), {"score": 0})
        tareas = CSTask.query.filter_by(account_id=acc.id, completada=False).order_by(CSTask.fecha_limite).all()
        owners = CSContacto.query.filter_by(account_id=acc.id, is_owner=True).all()
        account_data.append({
            "account": acc, "health": hs,
            "owners": owners, "tareas_pendientes": tareas,
        })

    # KPIs propios
    mrr_total = sum(float(a.mrr or 0) for a in accounts)
    arr_total = sum(float(a.arr_proyectado or 0) for a in accounts)
    cuentas_nuevas = sum(1 for a in accounts if a.es_cuenta_nueva)
    # FIX-2026-07-03: score puede ser None (sin datos suficientes); no cuenta como riesgo
    en_riesgo = sum(
        1 for a in accounts
        if (scores_map.get(str(a.id), {}).get("score") or 100) < 40
    )

    return render_template(
        "cs_mis_cuentas.html",
        accounts=account_data,
        mrr_total=mrr_total, arr_total=arr_total,
        cuentas_nuevas=cuentas_nuevas, en_riesgo=en_riesgo,
        total_cuentas=len(accounts),
        **_ctx(),
    )


# ══════════════════════════════════════════════
# MIS PENDIENTES Y TAREAS — agregador cross-modules
# ══════════════════════════════════════════════
@cs_bp.route("/mis-pendientes")
def mis_pendientes():
    """Todo lo asignado al usuario logueado: tareas, citas próximas,
    incidencias abiertas, alertas. Cross-account."""
    user_id = _current_user_id()
    user_nombre = session.get("user_nombre", "")
    if not user_id:
        return redirect(url_for("auth.login_page"))

    # Mis cuentas (para limitar tareas/citas a las que me corresponden)
    mis_account_ids = [a.id for a in CSAccount.query.filter_by(kam_id=user_id).all()]

    # Tasks pendientes
    tasks_q = CSTask.query.filter_by(completada=False)
    if mis_account_ids:
        # Tasks de mis cuentas O tasks donde responsable matchea mi nombre
        from sqlalchemy import or_ as _or
        tasks_q = tasks_q.filter(_or(
            CSTask.account_id.in_(mis_account_ids),
            CSTask.responsable.ilike(f"%{user_nombre}%") if user_nombre else False,
        ))
    elif user_nombre:
        tasks_q = tasks_q.filter(CSTask.responsable.ilike(f"%{user_nombre}%"))
    else:
        tasks_q = tasks_q.filter(False)  # no scope, empty

    tareas = tasks_q.order_by(CSTask.fecha_limite.asc().nullslast()).limit(200).all()
    accounts_for_tareas = {str(a.id): a for a in CSAccount.query.filter(
        CSAccount.id.in_([t.account_id for t in tareas])
    ).all()}

    # Citas próximas (próximos 14 días) de mis cuentas
    from datetime import timedelta as _td
    today_dt = datetime.utcnow()
    citas_proximas = []
    if mis_account_ids:
        citas_proximas = (
            CSAppointment.query
            .filter(CSAppointment.account_id.in_(mis_account_ids))
            .filter(CSAppointment.fecha_inicio >= today_dt)
            .filter(CSAppointment.fecha_inicio < today_dt + _td(days=14))
            .filter(~CSAppointment.estatus.in_(("Cancelada", "No Realizada", "Archivada")))
            .order_by(CSAppointment.fecha_inicio.asc()).limit(50).all()
        )
    accounts_for_citas = {str(a.id): a for a in CSAccount.query.filter(
        CSAccount.id.in_([c.account_id for c in citas_proximas])
    ).all()}

    # Incidencias abiertas
    incidencias = []
    if mis_account_ids:
        from models import CSIncidencia
        incidencias = (
            CSIncidencia.query
            .filter(CSIncidencia.account_id.in_(mis_account_ids))
            .filter(CSIncidencia.status != "Resuelta")
            .order_by(CSIncidencia.created_at.desc()).limit(50).all()
        )
    accounts_for_inc = {str(a.id): a for a in CSAccount.query.filter(
        CSAccount.id.in_([i.account_id for i in incidencias])
    ).all()}

    # Stats
    stats = {
        "total_tareas": len(tareas),
        "tareas_vencidas": sum(1 for t in tareas if t.fecha_limite and t.fecha_limite < today_dt.date()),
        "citas_proximas": len(citas_proximas),
        "incidencias_abiertas": len(incidencias),
        "mis_cuentas": len(mis_account_ids),
    }

    return render_template(
        "cs_mis_pendientes.html",
        tareas=tareas, accounts_for_tareas=accounts_for_tareas,
        citas_proximas=citas_proximas, accounts_for_citas=accounts_for_citas,
        incidencias=incidencias, accounts_for_inc=accounts_for_inc,
        stats=stats,
        **_ctx(),
    )


@cs_bp.route("/clientes/<uuid:account_id>/editar", methods=["POST"])
@require_cs_account_access
def editar_cliente(account_id):
    acc = _get_cs_account(account_id)
    # nombre se puede editar también si viene
    if "nombre" in request.form:
        new_nombre = request.form.get("nombre", "").strip()
        if new_nombre and new_nombre != acc.nombre:
            existing = CSAccount.query.filter(CSAccount.nombre == new_nombre, CSAccount.id != acc.id).first()
            if not existing:
                acc.nombre = new_nombre
    if "client_id" in request.form:
        new_cid = request.form.get("client_id", "").strip().upper()
        if new_cid and new_cid != acc.client_id:
            existing = CSAccount.query.filter(CSAccount.client_id == new_cid, CSAccount.id != acc.id).first()
            if not existing:
                acc.client_id = new_cid
    if "kam_id" in request.form:
        kam_id = request.form.get("kam_id", "").strip()
        if kam_id:
            acc.kam_id = kam_id
    if "logo_url" in request.form:
        acc.logo_url = request.form.get("logo_url", "").strip()
    if "giro" in request.form:
        giros = request.form.getlist("giro")
        acc.giro = ",".join(g.strip() for g in giros if g.strip())
    if "tier" in request.form:
        acc.tier = request.form.get("tier", "").strip()
    if "mrr" in request.form:
        try:
            acc.mrr = float(request.form.get("mrr") or 0)
        except (ValueError, TypeError):
            pass
    if "sucursales" in request.form:
        try:
            acc.sucursales = int(request.form.get("sucursales") or 0)
        except (ValueError, TypeError):
            pass
    if "unidades_contratadas" in request.form:
        unidades = request.form.getlist("unidades_contratadas")
        acc.unidades_contratadas = ",".join(u.strip() for u in unidades if u.strip())
    db.session.commit()
    return redirect(url_for("cs.clientes"))


@cs_bp.route("/clientes/crear", methods=["POST"])
@require_cs_admin
def crear_cliente():
    """Crear nueva CSAccount. Solo admin/director (no KAM)."""
    nombre = (request.form.get("nombre") or "").strip()
    if not nombre:
        return "nombre es requerido", 400
    if CSAccount.query.filter(CSAccount.nombre == nombre).first():
        return "Ya existe una cuenta con ese nombre", 400
    kam_id = request.form.get("kam_id", "").strip()
    if not kam_id:
        return "kam_id es requerido", 400
    # client_id auto-asignado por before_insert listener si viene vacío
    client_id = (request.form.get("client_id") or "").strip().upper() or None
    if client_id:
        existing = CSAccount.query.filter(CSAccount.client_id == client_id).first()
        if existing:
            return f"client_id {client_id} ya está en uso", 400

    try:
        mrr = float(request.form.get("mrr") or 0)
    except (ValueError, TypeError):
        mrr = 0
    try:
        sucursales = int(request.form.get("sucursales") or 0)
    except (ValueError, TypeError):
        sucursales = 0

    giros = request.form.getlist("giro")
    unidades = request.form.getlist("unidades_contratadas")

    acc = CSAccount(
        nombre=nombre, kam_id=kam_id,
        client_id=client_id,  # None → auto AX-XXXX por listener
        logo_url=request.form.get("logo_url", "").strip(),
        tier=request.form.get("tier", "").strip(),
        giro=",".join(g.strip() for g in giros if g.strip()),
        unidades_contratadas=",".join(u.strip() for u in unidades if u.strip()),
        mrr=mrr, sucursales=sucursales,
        es_cuenta_nueva=True,  # marca onboarding
    )
    db.session.add(acc)
    db.session.commit()
    return redirect(url_for("cs.clientes"))


@cs_bp.route("/clientes/<uuid:account_id>/eliminar", methods=["POST"])
@require_cs_full_access
def eliminar_cliente(account_id):
    """Elimina una CSAccount y todos sus registros relacionados.
    SOLO super_admin (no KAM, no director — es operación destructiva)."""
    acc = _get_cs_account(account_id)
    if not acc:
        return "No encontrada", 404
    # Cascade manual: borrar registros relacionados primero
    from models import (
        CSInvoice as _Inv, CSAppointment as _Apt, CSNote as _Note,
        CSTask as _Task, CSContacto as _Cnt, CSEntregable as _Ent,
        CSEncuesta as _Enc, CSIncidencia as _Inc, CSPropiedad as _Prop,
        CSOnboardingAccount as _On, CSOpportunity as _Opp,
    )
    nombre_borrado = acc.nombre
    for model_cls in (_Inv, _Apt, _Note, _Task, _Cnt, _Ent, _Enc, _Inc, _Prop, _On, _Opp):
        try:
            model_cls.query.filter_by(account_id=acc.id).delete(synchronize_session=False)
        except Exception:
            pass
    db.session.delete(acc)
    db.session.commit()
    from actividad import log_actividad
    try:
        log_actividad("eliminar", "cs_account", acc.id, f"Cuenta CS eliminada: {nombre_borrado}")
    except Exception:
        pass
    return redirect(url_for("cs.clientes"))


# ══════════════════════════════════════════════
# ACCOUNT DETAIL
# ══════════════════════════════════════════════
@cs_bp.route("/account/<uuid:account_id>")
@require_cs_account_access
def account_detail(account_id):
    inicio, fin, periodo_label, periodo_param = _get_periodo()

    account = _get_cs_account(account_id)
    if not account:
        return "Cuenta no encontrada", 404
    health = calcular_health_score(account)

    # Facturas del periodo seleccionado
    invoices = (
        CSInvoice.query.filter_by(account_id=account.id)
        .filter(CSInvoice.fecha_cobro >= inicio, CSInvoice.fecha_cobro < fin)
        .order_by(CSInvoice.fecha_cobro.desc()).all()
    )
    total_facturado = sum(float(i.total or 0) for i in invoices)
    total_pagado = sum(float(i.pagado or 0) for i in invoices)
    total_pendiente = sum(float(i.pendiente or 0) for i in invoices)
    facturas_pagadas = sum(1 for i in invoices if i.estatus == "Pagada")
    facturas_pendientes = sum(1 for i in invoices if i.estatus != "Pagada")

    # Facturación por UN
    def _classify_uen(uen):
        uen = (uen or "").upper().strip()
        if "AROMATEX" in uen:
            return "AROMATEX"
        elif "PESTEX" in uen:
            return "PESTEX"
        return "OTRO"

    fact_por_un = {"AROMATEX": {"facturado": 0, "pagado": 0, "pendiente": 0, "count": 0},
                   "PESTEX": {"facturado": 0, "pagado": 0, "pendiente": 0, "count": 0}}
    invoices_por_un = {"AROMATEX": [], "PESTEX": [], "OTRO": []}
    for inv in invoices:
        un = _classify_uen(inv.uen)
        invoices_por_un.setdefault(un, []).append(inv)
        if un in fact_por_un:
            fact_por_un[un]["facturado"] += float(inv.total or 0)
            fact_por_un[un]["pagado"] += float(inv.pagado or 0)
            fact_por_un[un]["pendiente"] += float(inv.pendiente or 0)
            fact_por_un[un]["count"] += 1

    # Citas por UN (Fumigación/Póliza = PESTEX, Aroma* = AROMATEX)
    def _classify_servicio(titulo):
        t = (titulo or "").lower()
        if "fumig" in t or "plaga" in t or "incidencia" not in t and "pestex" in t:
            return "PESTEX"
        elif "aroma" in t or "instalacion" in t:
            return "AROMATEX"
        elif "fumig" in t or "plaga" in t:
            return "PESTEX"
        return "OTRO"

    # Citas del periodo
    citas_estatus_rows = (
        db.session.query(CSAppointment.estatus, func.count(CSAppointment.id))
        .filter(
            CSAppointment.account_id == account.id,
            CSAppointment.fecha_inicio >= inicio,
            CSAppointment.fecha_inicio < fin,
        )
        .group_by(CSAppointment.estatus)
        .all()
    )
    citas_por_estatus = {estatus: cnt for estatus, cnt in citas_estatus_rows}

    appointments = (
        CSAppointment.query.filter(
            CSAppointment.account_id == account.id,
            CSAppointment.fecha_inicio >= inicio,
            CSAppointment.fecha_inicio < fin,
        )
        .order_by(CSAppointment.fecha_inicio.desc()).limit(200).all()
    )

    # Sucursales por UN para esta cuenta
    from sqlalchemy import case as sa_case, distinct
    suc_un = db.session.query(
        func.count(distinct(sa_case(
            (CSAppointment.titulo_servicio.ilike("%aroma%"), CSAppointment.propiedad),
            (CSAppointment.titulo_servicio.ilike("%instalacion%"), CSAppointment.propiedad),
        ))),
        func.count(distinct(sa_case(
            (CSAppointment.titulo_servicio.ilike("%fumig%"), CSAppointment.propiedad),
            (CSAppointment.titulo_servicio.ilike("%plaga%"), CSAppointment.propiedad),
        ))),
    ).filter(CSAppointment.account_id == account.id).first()
    suc_aromatex = suc_un[0] if suc_un else 0
    suc_pestex = suc_un[1] if suc_un else 0

    # Citas agrupadas por UN (query aggregate, sin limit)
    _is_aro = db.or_(CSAppointment.titulo_servicio.ilike("%aroma%"), CSAppointment.titulo_servicio.ilike("%instalacion%"))
    _is_pest = db.or_(CSAppointment.titulo_servicio.ilike("%fumig%"), CSAppointment.titulo_servicio.ilike("%plaga%"), CSAppointment.titulo_servicio.ilike("%pestex%"))
    citas_un_row = db.session.query(
        func.sum(sa_case((_is_aro, 1), else_=0)),
        func.sum(sa_case((db.and_(_is_aro, CSAppointment.estatus == "Terminada"), 1), else_=0)),
        func.sum(sa_case((_is_pest, 1), else_=0)),
        func.sum(sa_case((db.and_(_is_pest, CSAppointment.estatus == "Terminada"), 1), else_=0)),
    ).filter(
        CSAppointment.account_id == account.id,
        CSAppointment.fecha_inicio >= inicio,
        CSAppointment.fecha_inicio < fin,
    ).first()
    citas_por_un = {
        "AROMATEX": {"total": int(citas_un_row[0] or 0), "terminadas": int(citas_un_row[1] or 0)},
        "PESTEX": {"total": int(citas_un_row[2] or 0), "terminadas": int(citas_un_row[3] or 0)},
    }

    notes = CSNote.query.filter_by(account_id=account.id).order_by(CSNote.created_at.desc()).all()
    tasks = CSTask.query.filter_by(account_id=account.id).order_by(CSTask.completada, CSTask.fecha_limite).all()
    tareas_pendientes = sum(1 for t in tasks if not t.completada)
    contactos = CSContacto.query.filter_by(account_id=account.id).order_by(CSContacto.is_owner.desc(), CSContacto.nombre).all()
    entregables = CSEntregable.query.filter_by(account_id=account.id).order_by(CSEntregable.unidad_negocio, CSEntregable.orden).all()
    entregables_por_un = {}
    for e in entregables:
        un = e.unidad_negocio or "General"
        entregables_por_un.setdefault(un, []).append(e)
    workload_survey = CSWorkloadSurvey.query.filter_by(
        account_id=account.id,
        periodo=periodo_param,
    ).first()
    workload_activity_values = {
        item.get("actividad"): item.get("horas", "No aplica")
        for item in (workload_survey.actividades_horas if workload_survey else [])
        if isinstance(item, dict)
    }
    workload_custom_activities = [
        item for item in (workload_survey.actividades_horas if workload_survey else [])
        if isinstance(item, dict) and item.get("actividad") not in WORKLOAD_ACTIVITIES
    ]
    workload_entregable_otro = ""
    if workload_survey and workload_survey.entregables_tipos:
        for item in workload_survey.entregables_tipos:
            if isinstance(item, str) and item.startswith("Otro:"):
                workload_entregable_otro = item.replace("Otro:", "", 1).strip()
                break
    workload_bloqueo_otro = ""
    if workload_survey and (workload_survey.tipo_bloqueo or "").startswith("Otro:"):
        workload_bloqueo_otro = workload_survey.tipo_bloqueo.replace("Otro:", "", 1).strip()

    # Incidencias
    incidencias = CSIncidencia.query.filter_by(account_id=account.id).order_by(CSIncidencia.created_at.desc()).limit(100).all()
    propiedades = CSPropiedad.query.filter_by(account_id=account.id).order_by(CSPropiedad.nombre).all()

    # Encuestas NPS/CSAT
    encuestas = CSEncuesta.query.filter_by(account_id=account.id).order_by(CSEncuesta.created_at.desc()).all()
    survey_link = f"/encuesta/{account.survey_token}" if account.survey_token else None

    # Calcular promedios NPS + CSAT (6 dimensiones)
    def _avg(field):
        vals = [getattr(e, field) for e in encuestas if getattr(e, field) is not None]
        return round(sum(vals) / len(vals), 1) if vals else None

    if encuestas:
        avg_nps = _avg("nps")
        # CSAT promedio de las 6 dimensiones
        csat_dims = {}
        for dim in ["csat", "csat_calidad", "csat_respuesta", "csat_comunicacion", "csat_precio", "csat_tecnico"]:
            csat_dims[dim] = _avg(dim)
        csat_vals = [v for v in csat_dims.values() if v is not None]
        avg_csat = round(sum(csat_vals) / len(csat_vals), 1) if csat_vals else None

        # KPI combinado: NPS (0-10) + CSAT normalizado (1-5 → 0-10) → promedio
        if avg_nps is not None and avg_csat is not None:
            csat_normalized = (avg_csat - 1) / 4 * 10
            kpi_satisfaccion = round((avg_nps + csat_normalized) / 2, 1)
        else:
            kpi_satisfaccion = round(avg_nps, 1) if avg_nps else None
    else:
        avg_nps = avg_csat = kpi_satisfaccion = None
        csat_dims = {}

    return render_template(
        "cs_account_detail.html",
        account=account, health=health, invoices=invoices,
        total_facturado=total_facturado, total_pagado=total_pagado,
        total_pendiente=total_pendiente, facturas_pagadas=facturas_pagadas,
        facturas_pendientes=facturas_pendientes,
        fact_por_un=fact_por_un, invoices_por_un=invoices_por_un,
        citas_por_un=citas_por_un,
        suc_aromatex=suc_aromatex, suc_pestex=suc_pestex,
        appointments=appointments, citas_por_estatus=citas_por_estatus,
        notes=notes, tasks=tasks, tareas_pendientes=tareas_pendientes,
        contactos=contactos,
        entregables=entregables, entregables_por_un=entregables_por_un,
        workload_survey=workload_survey,
        workload_activity_values=workload_activity_values,
        workload_custom_activities=workload_custom_activities,
        workload_entregable_otro=workload_entregable_otro,
        workload_bloqueo_otro=workload_bloqueo_otro,
        workload_activities=WORKLOAD_ACTIVITIES,
        workload_options=WORKLOAD_SURVEY_OPTIONS,
        incidencias=incidencias, propiedades=propiedades,
        encuestas=encuestas, survey_link=survey_link,
        avg_nps=avg_nps, avg_csat=avg_csat, kpi_satisfaccion=kpi_satisfaccion,
        csat_dims=csat_dims,
        today=date.today(), account_alerts=alertas_por_cuenta(str(account_id)),
        kams=_get_kams(),
        periodo_label=periodo_label, periodo_param=periodo_param,
        periodos=_periodos_disponibles(),
        **_ctx(),
    )


# ══════════════════════════════════════════════
# KAM SCORECARD
# ══════════════════════════════════════════════
@cs_bp.route("/kam-scorecard")
def kam_scorecard():
    inicio, fin, periodo_label, periodo_param = _get_periodo()
    rows, totals = _build_kam_scorecard(inicio, fin)
    return render_template(
        "cs_kam_scorecard.html",
        rows=rows,
        totals=totals,
        periodo_label=periodo_label,
        periodo_param=periodo_param,
        periodos=_periodos_disponibles(),
        **_ctx(),
    )


# ══════════════════════════════════════════════
# CARGA OPERATIVA
# ══════════════════════════════════════════════
@cs_bp.route("/carga-operativa")
def carga_operativa():
    inicio, fin, periodo_label, periodo_param = _get_periodo()
    data = _build_carga_operativa(periodo_param)
    return render_template(
        "cs_carga_operativa.html",
        periodo_label=periodo_label,
        periodo_param=periodo_param,
        periodos=_periodos_disponibles(),
        **data,
        **_ctx(),
    )


# ══════════════════════════════════════════════
# KAM VIEW
# ══════════════════════════════════════════════
@cs_bp.route("/kam")
@cs_bp.route("/kam/<uuid:kam_id>")
def kam_view(kam_id=None):
    kams = _get_kams()
    if kam_id is None:
        if _is_kam():
            kam_id = session.get("user_id")
        elif kams:
            kam_id = kams[0].id

    kam = db.session.get(UserCRM, kam_id)
    if not kam:
        return "KAM no encontrado", 404

    # FEAT-2026-06-29: filtro global por UN también dentro de kam_view
    from un_filter import filtrar_cs_accounts_por_un
    q = CSAccount.query.filter_by(kam_id=kam.id)
    q = filtrar_cs_accounts_por_un(q, CSAccount, request.args.get("un"))
    accounts = q.order_by(CSAccount.mrr.desc()).all()
    scores_map = calcular_health_scores_batch(accounts)

    account_scores = []
    for acc in accounts:
        hs = scores_map[str(acc.id)]
        tareas = CSTask.query.filter_by(account_id=acc.id, completada=False).order_by(CSTask.fecha_limite).all()
        account_scores.append({"account": acc, "health": hs, "tareas_pendientes": tareas})

    mrr_kam = sum(float(a.mrr or 0) for a in accounts)
    arr_kam = sum(float(a.arr_proyectado or 0) for a in accounts)
    sucursales_kam = sum(a.sucursales for a in accounts)
    avg_score = sum(i["health"]["score"] for i in account_scores) / len(account_scores) if account_scores else 0

    if len(account_scores) > 1:
        scores = [i["health"]["score"] for i in account_scores]
        mean = sum(scores) / len(scores)
        variance = sum((s - mean) ** 2 for s in scores) / len(scores)
        balance_score = max(0, 100 - variance ** 0.5)
    else:
        balance_score = 100

    todas_tareas = []
    for item in account_scores:
        for t in item["tareas_pendientes"]:
            todas_tareas.append({"tarea": t, "cuenta": item["account"].nombre})

    return render_template(
        "cs_kam_view.html",
        kams=kams, kam=kam, account_scores=account_scores,
        mrr_kam=mrr_kam, arr_kam=arr_kam, sucursales_kam=sucursales_kam,
        avg_score=avg_score, balance_score=balance_score,
        todas_tareas=todas_tareas, **_ctx(),
    )


# ══════════════════════════════════════════════
# ALERTAS
# ══════════════════════════════════════════════
@cs_bp.route("/alertas")
def alertas_view():
    accounts = CSAccount.query.filter(db.or_(CSAccount.en_due_diligence.is_(None), CSAccount.en_due_diligence.is_(False))).all()
    scores_map = calcular_health_scores_batch(accounts)
    alertas = generar_alertas(accounts=accounts, scores_map=scores_map)
    por_severidad = {"critica": [], "alta": [], "media": []}
    for a in alertas:
        por_severidad[a["severidad"]].append(a)
    por_kam = {}
    for a in alertas:
        por_kam.setdefault(a["kam"], []).append(a)
    return render_template(
        "cs_alertas.html",
        alertas=alertas, por_severidad=por_severidad, por_kam=por_kam,
        **_ctx(),
    )


# ══════════════════════════════════════════════
# OPORTUNIDADES
# ══════════════════════════════════════════════
@cs_bp.route("/oportunidades")
def oportunidades():
    pipeline = {}
    for key, label in ETAPAS_PIPELINE:
        opps = CSOpportunity.query.filter_by(etapa=key).order_by(CSOpportunity.created_at.desc()).all()
        pipeline[key] = {"label": label, "opps": opps}

    total_opps = CSOpportunity.query.count()
    valor_pipeline = db.session.query(
        func.coalesce(func.sum(CSOpportunity.valor_estimado), 0)
    ).filter(CSOpportunity.etapa.notin_(["ganada", "perdida"])).scalar()
    ganadas = CSOpportunity.query.filter_by(etapa="ganada").count()
    valor_ganado = db.session.query(
        func.coalesce(func.sum(CSOpportunity.valor_estimado), 0)
    ).filter_by(etapa="ganada").scalar()

    accounts = CSAccount.query.filter(db.or_(CSAccount.en_due_diligence.is_(None), CSAccount.en_due_diligence.is_(False))).order_by(CSAccount.nombre).all()
    kams = _get_kams()

    return render_template(
        "cs_oportunidades.html",
        pipeline=pipeline, etapas=ETAPAS_PIPELINE,
        tipos=TIPOS_OPORTUNIDAD,
        total_opps=total_opps, valor_pipeline=float(valor_pipeline),
        ganadas=ganadas, valor_ganado=float(valor_ganado),
        accounts=accounts, kams=kams, **_ctx(),
    )


@cs_bp.route("/oportunidades/crear", methods=["POST"])
def crear_oportunidad():
    account_id = request.form.get("account_id", "").strip()
    if account_id:
        account = _get_cs_account(account_id)
        err = _require_account_access(account)
        if err:
            return err
    elif not _is_cs_admin() and request.form.get("kam_id") != str(_current_kam_id()):
        return _permission_denied()
    opp = CSOpportunity(
        account_id=account_id if account_id else None,
        prospecto_nombre=request.form.get("prospecto_nombre", "").strip(),
        contacto=request.form.get("contacto", "").strip(),
        contacto_telefono=request.form.get("contacto_telefono", "").strip(),
        contacto_email=request.form.get("contacto_email", "").strip(),
        tipo=request.form.get("tipo", "upsell_un"),
        unidad_negocio=request.form.get("unidad_negocio", ""),
        descripcion=request.form.get("descripcion", "").strip(),
        valor_estimado=float(request.form.get("valor_estimado", 0) or 0),
        etapa="prospeccion",
        kam_id=request.form.get("kam_id") or None,
    )
    db.session.add(opp)
    db.session.commit()
    return redirect(url_for("cs.oportunidades"))


@cs_bp.route("/oportunidades/<uuid:opp_id>/etapa", methods=["POST"])
def cambiar_etapa(opp_id):
    opp = db.session.get(CSOpportunity, opp_id)
    err = _require_opportunity_access(opp)
    if err:
        return err
    nueva = request.form.get("etapa", "")
    if nueva in [e[0] for e in ETAPAS_PIPELINE]:
        opp.etapa = nueva
        db.session.commit()
    return redirect(url_for("cs.oportunidades"))


@cs_bp.route("/oportunidades/<uuid:opp_id>/editar", methods=["POST"])
def editar_oportunidad(opp_id):
    """Edita todos los campos del detalle de una oportunidad."""
    opp = db.session.get(CSOpportunity, opp_id)
    err = _require_opportunity_access(opp)
    if err:
        return err
    f = request.form
    acc_id = (f.get("account_id") or "").strip()
    if acc_id:
        account = _get_cs_account(acc_id)
        err = _require_account_access(account)
        if err:
            return err
    opp.account_id = acc_id if acc_id else None
    opp.prospecto_nombre = (f.get("prospecto_nombre") or "").strip()
    opp.contacto = (f.get("contacto") or "").strip()
    opp.contacto_telefono = (f.get("contacto_telefono") or "").strip()
    opp.contacto_email = (f.get("contacto_email") or "").strip()
    opp.tipo = f.get("tipo") or opp.tipo
    opp.unidad_negocio = (f.get("unidad_negocio") or "").strip()
    opp.descripcion = (f.get("descripcion") or "").strip()
    try:
        opp.valor_estimado = float(f.get("valor_estimado") or 0)
    except (ValueError, TypeError):
        pass
    nueva_etapa = f.get("etapa") or opp.etapa
    if nueva_etapa in [e[0] for e in ETAPAS_PIPELINE]:
        opp.etapa = nueva_etapa
    opp.kam_id = f.get("kam_id") or None
    db.session.commit()
    return redirect(url_for("cs.oportunidades"))


@cs_bp.route("/oportunidades/<uuid:opp_id>/delete", methods=["POST"])
def eliminar_oportunidad(opp_id):
    opp = db.session.get(CSOpportunity, opp_id)
    err = _require_opportunity_access(opp)
    if err:
        return err
    db.session.delete(opp)
    db.session.commit()
    return redirect(url_for("cs.oportunidades"))


# ══════════════════════════════════════════════
# ONBOARDING
# ══════════════════════════════════════════════
@cs_bp.route("/onboarding")
def onboarding():
    cuentas_nuevas = CSAccount.query.filter_by(es_cuenta_nueva=True).all()
    scores_map = calcular_health_scores_batch(cuentas_nuevas)
    cuentas_nuevas_data = []
    for acc in cuentas_nuevas:
        hs = scores_map[str(acc.id)]
        tareas = CSTask.query.filter_by(account_id=acc.id, completada=False).all()
        cuentas_nuevas_data.append({"account": acc, "health": hs, "tareas": tareas})

    pipeline = CSOnboardingAccount.query.all()
    kams = _get_kams()

    return render_template(
        "cs_onboarding.html",
        cuentas_nuevas=cuentas_nuevas_data,
        pipeline=pipeline, kams=kams, **_ctx(),
    )


@cs_bp.route("/onboarding/<uuid:ob_id>/asignar", methods=["POST"])
@require_cs_admin
def asignar_kam_onboarding(ob_id):
    ob = db.session.get(CSOnboardingAccount, ob_id)
    if ob:
        kam_id = request.form.get("kam_id", "").strip()
        ob.kam_id = kam_id if kam_id else None
        db.session.commit()
    return redirect(url_for("cs.onboarding"))


# ══════════════════════════════════════════════
# API — Chart data (JSON)
# ══════════════════════════════════════════════
@cs_bp.route("/api/mrr-trend")
def api_mrr_trend():
    """MRR facturado por mes. Filtrable por ?account_id= o ?kam_id="""
    from sqlalchemy import case
    q = (
        db.session.query(
            func.date_trunc("month", CSInvoice.fecha_cobro).label("mes"),
            func.sum(CSInvoice.total),
            func.sum(CSInvoice.pagado),
            func.sum(CSInvoice.pendiente),
        )
        .filter(CSInvoice.fecha_cobro.isnot(None))
    )

    account_id = request.args.get("account_id")
    kam_id = request.args.get("kam_id")
    if account_id:
        q = q.filter(CSInvoice.account_id == account_id)
    elif kam_id:
        kam_accounts = [a.id for a in CSAccount.query.filter_by(kam_id=kam_id).all()]
        if kam_accounts:
            q = q.filter(CSInvoice.account_id.in_(kam_accounts))
    elif _is_kam():
        kam_accounts = [a.id for a in CSAccount.query.filter_by(kam_id=_current_kam_id()).all()]
        if kam_accounts:
            q = q.filter(CSInvoice.account_id.in_(kam_accounts))

    rows = q.group_by("mes").order_by("mes").all()
    return jsonify([{
        "mes": r[0].strftime("%Y-%m") if r[0] else "",
        "mes_label": r[0].strftime("%b %Y") if r[0] else "",
        "facturado": float(r[1] or 0),
        "pagado": float(r[2] or 0),
        "pendiente": float(r[3] or 0),
    } for r in rows])


@cs_bp.route("/api/mrr-trend-un")
def api_mrr_trend_un():
    """MRR facturado por mes dividido por UN (AROMATEX vs PESTEX)."""
    from sqlalchemy import case
    account_id = request.args.get("account_id")
    q = db.session.query(
        func.date_trunc("month", CSInvoice.fecha_cobro).label("mes"),
        func.sum(case((CSInvoice.uen.ilike("%AROMATEX%"), CSInvoice.total), else_=0)).label("aromatex"),
        func.sum(case((CSInvoice.uen.ilike("%PESTEX%"), CSInvoice.total), else_=0)).label("pestex"),
        func.sum(CSInvoice.total).label("total"),
    ).filter(CSInvoice.fecha_cobro.isnot(None))

    if account_id:
        q = q.filter(CSInvoice.account_id == account_id)

    rows = q.group_by("mes").order_by("mes").all()
    return jsonify([{
        "mes": r[0].strftime("%Y-%m") if r[0] else "",
        "mes_label": r[0].strftime("%b %Y") if r[0] else "",
        "aromatex": float(r.aromatex or 0),
        "pestex": float(r.pestex or 0),
        "total": float(r.total or 0),
    } for r in rows])


@cs_bp.route("/api/operacion-trend")
def api_operacion_trend():
    """Citas por estatus por mes. Filtrable por ?account_id= o ?kam_id="""
    from sqlalchemy import case
    q = (
        db.session.query(
            func.date_trunc("month", CSAppointment.fecha_inicio).label("mes"),
            func.count(CSAppointment.id).label("total"),
            func.sum(case((CSAppointment.estatus == "Terminada", 1), else_=0)).label("terminadas"),
            func.sum(case((CSAppointment.estatus == "Cancelada", 1), else_=0)).label("canceladas"),
            func.sum(case((CSAppointment.estatus == "No Realizada", 1), else_=0)).label("no_realizadas"),
        )
        .filter(CSAppointment.fecha_inicio.isnot(None))
    )

    account_id = request.args.get("account_id")
    kam_id = request.args.get("kam_id")
    if account_id:
        q = q.filter(CSAppointment.account_id == account_id)
    elif kam_id:
        kam_accounts = [a.id for a in CSAccount.query.filter_by(kam_id=kam_id).all()]
        if kam_accounts:
            q = q.filter(CSAppointment.account_id.in_(kam_accounts))
    elif _is_kam():
        kam_accounts = [a.id for a in CSAccount.query.filter_by(kam_id=_current_kam_id()).all()]
        if kam_accounts:
            q = q.filter(CSAppointment.account_id.in_(kam_accounts))

    rows = q.group_by("mes").order_by("mes").all()
    return jsonify([{
        "mes": r[0].strftime("%Y-%m") if r[0] else "",
        "mes_label": r[0].strftime("%b %Y") if r[0] else "",
        "total": int(r.total),
        "terminadas": int(r.terminadas or 0),
        "canceladas": int(r.canceladas or 0),
        "no_realizadas": int(r.no_realizadas or 0),
        "pct_cumplimiento": round(int(r.terminadas or 0) / int(r.total) * 100, 1) if r.total > 0 else 0,
    } for r in rows])


@cs_bp.route("/api/email-response-times")
def api_email_response_times():
    """Métricas de tiempo de respuesta a email por KAM (últimos 30 días).

    FEAT-2026-07-03: retorna 4 métricas por KAM + agregado global:
      - median_hours: mediana (métrica primaria)
      - avg_hours: promedio (sensible a outliers)
      - distribucion: %<2h, %<24h, %>24h
      - pendientes: threads con email cliente en últimas 72h sin respuesta
    """
    from models import KAMEmailResponse
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import func as sqlfunc

    now = datetime.now(timezone.utc)
    cutoff_30d = now - timedelta(days=30)
    cutoff_72h = now - timedelta(hours=72)

    rows = (
        db.session.query(
            KAMEmailResponse.kam_id,
            sqlfunc.count(KAMEmailResponse.id).label("n"),
            sqlfunc.avg(KAMEmailResponse.response_hours).label("avg_hours"),
            sqlfunc.percentile_cont(0.5).within_group(
                KAMEmailResponse.response_hours
            ).label("median_hours"),
            sqlfunc.sum(sqlfunc.cast(
                KAMEmailResponse.response_hours <= 2, db.Integer
            )).label("bucket_2h"),
            sqlfunc.sum(sqlfunc.cast(
                KAMEmailResponse.response_hours <= 24, db.Integer
            )).label("bucket_24h"),
        )
        .filter(KAMEmailResponse.replied_at >= cutoff_30d)
        .group_by(KAMEmailResponse.kam_id)
        .all()
    )

    # Pendientes: emails de clientes recibidos en las últimas 72h sin
    # respuesta posterior del KAM. Los inferimos como threads en
    # KAMEmailResponse cuyo received_at está en la ventana pero cuyo
    # replied_at es NULL — hoy no guardamos NULL, así que también contamos
    # threads que ya fueron respondidos pero llegaron nuevos emails después
    # (proxy razonable hasta implementar tabla de pendientes explícita).
    pendientes_rows = (
        db.session.query(
            KAMEmailResponse.kam_id,
            sqlfunc.count(KAMEmailResponse.id).label("n"),
        )
        .filter(
            KAMEmailResponse.received_at >= cutoff_72h,
            KAMEmailResponse.response_hours > 24,
        )
        .group_by(KAMEmailResponse.kam_id)
        .all()
    )
    pendientes_by_kam = {str(r.kam_id): int(r.n) for r in pendientes_rows}

    kams = {str(k.id): k.nombre for k in _get_kams()}
    data_by_kam = {str(r.kam_id): r for r in rows}

    def _pct(part, total):
        if not total: return 0.0
        return round(float(part) / float(total) * 100, 1)

    result = []
    total_n = total_h_sum = 0
    for kid, kname in sorted(kams.items(), key=lambda x: x[1]):
        r = data_by_kam.get(kid)
        n_emails = int(r.n) if r else 0
        b2h  = int(r.bucket_2h  or 0) if r else 0
        b24h = int(r.bucket_24h or 0) if r else 0
        result.append({
            "kam_id":       kid,
            "kam_nombre":   kname,
            "n_emails":     n_emails,
            "avg_hours":    round(float(r.avg_hours), 1) if r and r.avg_hours is not None else None,
            "median_hours": round(float(r.median_hours), 1) if r and r.median_hours is not None else None,
            "pct_bajo_2h":  _pct(b2h, n_emails),
            "pct_bajo_24h": _pct(b24h, n_emails),
            "pct_sobre_24h": _pct(n_emails - b24h, n_emails),
            "pendientes":   pendientes_by_kam.get(kid, 0),
        })
        if r:
            total_n += n_emails
            total_h_sum += float(r.avg_hours or 0) * n_emails

    # Agregado global (ponderado por # emails)
    all_hours = [x.get("median_hours") for x in result if x.get("median_hours") is not None]
    global_median = round(sorted(all_hours)[len(all_hours)//2], 1) if all_hours else None
    global_avg = round(total_h_sum / total_n, 1) if total_n else None
    total_pendientes = sum(x["pendientes"] for x in result)

    # Distribución global (recalculada de suma directa)
    dist_row = (
        db.session.query(
            sqlfunc.count(KAMEmailResponse.id).label("n"),
            sqlfunc.sum(sqlfunc.cast(KAMEmailResponse.response_hours <= 2, db.Integer)).label("b2"),
            sqlfunc.sum(sqlfunc.cast(KAMEmailResponse.response_hours <= 24, db.Integer)).label("b24"),
        )
        .filter(KAMEmailResponse.replied_at >= cutoff_30d)
        .first()
    )
    gn  = int(dist_row.n or 0)
    gb2 = int(dist_row.b2 or 0)
    gb24= int(dist_row.b24 or 0)

    return jsonify({
        "por_kam": result,
        "global": {
            "median_hours":  global_median,
            "avg_hours":     global_avg,
            "n_emails":      gn,
            "pct_bajo_2h":   _pct(gb2, gn),
            "pct_bajo_24h":  _pct(gb24, gn),
            "pct_sobre_24h": _pct(gn - gb24, gn),
            "pendientes":    total_pendientes,
        },
    })


@cs_bp.route("/api/cuentas-sin-sub-savio", methods=["GET"])
def cuentas_sin_sub_savio():
    """FEAT-2026-07-03: cuentas con MRR contratado=0 pero MRR observado>0.

    Detecta cuentas cuyo cs_accounts.mrr (contratado, viene de subs Savio)
    quedó en 0 mientras que mrr_observado (facturación recurrente real
    últimos 5m) tiene valor. Indica un customer en Savio sin subscription
    activa registrada, aunque el negocio sí factura recurrentemente.

    Alcance del bug detectado por Diego (2026-07-03): 4 cuentas por
    $458k/mes de MRR real no reflejado en Savio.
    """
    from sqlalchemy import or_
    rows = (
        CSAccount.query
        .filter(or_(CSAccount.mrr.is_(None), CSAccount.mrr == 0))
        .filter(CSAccount.mrr_observado > 0)
        .order_by(CSAccount.mrr_observado.desc())
        .all()
    )
    kams_by_id = {str(k.id): k.nombre for k in _get_kams()}
    out = []
    total_mrr = 0.0
    for a in rows:
        mrr_obs = float(a.mrr_observado or 0)
        total_mrr += mrr_obs
        out.append({
            "id": str(a.id),
            "nombre": a.nombre,
            "client_id": a.client_id,
            "kam": kams_by_id.get(str(a.kam_id), "—"),
            "mrr_contratado": float(a.mrr or 0),
            "mrr_observado": mrr_obs,
            "facturacion_q1": float(a.facturacion_q1 or 0),
            "pagado_q1": float(a.pagado_q1 or 0),
            "unidades": a.unidades_contratadas or "",
        })
    return jsonify({
        "cuentas": out,
        "total": len(out),
        "mrr_no_reflejado": round(total_mrr, 2),
        "arr_no_reflejado": round(total_mrr * 12, 2),
    })


@cs_bp.route("/api/zoho/diagnose", methods=["POST"])
@require_cs_admin
def zoho_diagnose():
    """FEAT-2026-07-07: Diagnóstico del error 'invalid_code' — prueba las 4
    regiones de Zoho con las credenciales configuradas en Render.

    Retorna qué región (si alguna) acepta el refresh_token actual, y para
    las que fallan muestra el mensaje exacto de Zoho para debug.
    """
    import os as _os
    import requests as _rq

    refresh = _os.getenv("ZOHO_REFRESH_TOKEN")
    cid     = _os.getenv("ZOHO_CLIENT_ID")
    csec    = _os.getenv("ZOHO_CLIENT_SECRET")
    if not (refresh and cid and csec):
        return jsonify({
            "error": "Faltan credenciales base",
            "faltantes": [k for k, v in [
                ("ZOHO_REFRESH_TOKEN", refresh),
                ("ZOHO_CLIENT_ID", cid),
                ("ZOHO_CLIENT_SECRET", csec),
            ] if not v],
        }), 400

    dominios = [
        ("accounts.zoho.com",    "Global (US)"),
        ("accounts.zoho.eu",     "Europa"),
        ("accounts.zoho.in",     "India"),
        ("accounts.zoho.com.au", "Australia"),
        ("accounts.zoho.jp",     "Japón"),
    ]

    resultados = []
    region_ok = None
    for dominio, region in dominios:
        try:
            r = _rq.post(
                f"https://{dominio}/oauth/v2/token",
                data={
                    "grant_type":    "refresh_token",
                    "refresh_token": refresh,
                    "client_id":     cid,
                    "client_secret": csec,
                },
                timeout=12,
            )
            body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {"raw": r.text[:200]}
            if body.get("access_token"):
                resultados.append({
                    "region":   region,
                    "dominio":  dominio,
                    "ok":       True,
                    "detalle":  f"expires_in={body.get('expires_in')}s",
                })
                if not region_ok:
                    region_ok = dominio
            else:
                resultados.append({
                    "region":   region,
                    "dominio":  dominio,
                    "ok":       False,
                    "detalle":  body.get("error") or str(body)[:100],
                })
        except Exception as e:
            resultados.append({
                "region": region, "dominio": dominio, "ok": False,
                "detalle": f"{type(e).__name__}: {str(e)[:80]}",
            })

    # Detectar el problema más probable
    todos_invalid_code = all(not r["ok"] and r["detalle"] == "invalid_code" for r in resultados)
    algun_invalid_client = any(r["detalle"] == "invalid_client" for r in resultados)

    diagnostico = None
    accion = None
    if region_ok:
        actual_dominio = _os.getenv("ZOHO_ACCOUNTS_DOMAIN", "accounts.zoho.com")
        if region_ok != actual_dominio:
            diagnostico = f"✓ El refresh_token funciona en {region_ok}, pero tu ETL está apuntando a {actual_dominio}"
            accion = f"Agrega en Render: ZOHO_ACCOUNTS_DOMAIN={region_ok}"
        else:
            diagnostico = f"✓ Todo bien: refresh_token válido en {region_ok} (la región que ya usas)"
            accion = "El error 'invalid_code' del sync viene de otra causa — revisa logs de Render"
    elif algun_invalid_client:
        diagnostico = "✗ ZOHO_CLIENT_ID + ZOHO_CLIENT_SECRET no matchean con el refresh_token"
        accion = "Regenera el refresh_token con el MISMO Self-Client cuyo Client ID/Secret están en Render"
    elif todos_invalid_code:
        diagnostico = "✗ El refresh_token es inválido (revocado o expirado) en TODAS las regiones"
        accion = "Genera un nuevo refresh_token desde Zoho API Console (Self-Client → Generate Code)"
    else:
        diagnostico = "✗ Ninguna región aceptó el token; revisa el detalle por región"
        accion = "Verifica que copiaste el refresh_token completo sin espacios"

    return jsonify({
        "diagnostico":       diagnostico,
        "accion_sugerida":   accion,
        "region_correcta":   region_ok,
        "dominio_configurado": _os.getenv("ZOHO_ACCOUNTS_DOMAIN", "accounts.zoho.com"),
        "por_region":        resultados,
    })


@cs_bp.route("/api/zoho/sync-appointments", methods=["POST"])
@require_cs_admin
def zoho_sync_appointments():
    """Trigger manual del ETL Zoho Analytics → cs_appointments (super_admin).

    FEAT-2026-07-03: Diego pidió forzar la conexión. Este endpoint dispara
    zoho_appointments_etl.run() en el mismo proceso Flask.

    Requiere env vars en Render:
      ZOHO_CLIENT_ID, ZOHO_CLIENT_SECRET, ZOHO_REFRESH_TOKEN,
      ZOHO_USER_EMAIL, ZOHO_WORKSPACE, ZOHO_TABLE,
      SUPABASE_URL, SUPABASE_SERVICE_KEY
    """
    import os as _os
    faltan = [k for k in ("ZOHO_CLIENT_ID","ZOHO_CLIENT_SECRET","ZOHO_REFRESH_TOKEN",
                          "ZOHO_USER_EMAIL","ZOHO_WORKSPACE","ZOHO_TABLE",
                          "SUPABASE_URL","SUPABASE_SERVICE_KEY") if not _os.getenv(k)]
    if faltan:
        return jsonify({
            "error": "Faltan env vars en Render",
            "faltantes": faltan,
        }), 400
    try:
        import zoho_appointments_etl as etl
        result = etl.run()
        return jsonify({"ok": True, **result})
    except KeyError as e:
        return jsonify({"error": f"Falta env var: {e}"}), 400
    except Exception as e:
        import traceback
        return jsonify({
            "error": f"{type(e).__name__}: {e}",
            "traceback": traceback.format_exc()[-800:],
        }), 500


@cs_bp.route("/api/email-response-times/resync", methods=["POST"])
@require_cs_admin
def resync_kam_responses():
    """Trigger manual del polling KAM (solo super_admin). FEAT-2026-07-03."""
    try:
        import gmail_monitor
        days = int(request.args.get("days", 30))
        result = gmail_monitor.poll_kam_responses(lookback_days=days)
        return jsonify(result)
    except Exception as e:
        import traceback
        return jsonify({
            "error": f"{type(e).__name__}: {e}",
            "traceback": traceback.format_exc()[-500:],
        }), 500


# ══════════════════════════════════════════════
# CARGA DE DATOS — CSV upload
# ══════════════════════════════════════════════
def _parse_money(val):
    if not val or val == "nan":
        return 0.0
    return float(str(val).replace("$", "").replace(",", "").strip() or 0)


def _parse_date_cobros(val):
    """Parsea '31 mar 2026' o '15 abr 2026'."""
    if not val or val == "nan" or str(val).strip() == "":
        return None
    import locale
    for fmt in ("%d %b %Y", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(val).strip(), fmt).date()
        except ValueError:
            continue
    # Intentar con meses en español
    meses_es = {
        "ene": "01", "feb": "02", "mar": "03", "abr": "04",
        "may": "05", "jun": "06", "jul": "07", "ago": "08",
        "sep": "09", "oct": "10", "nov": "11", "dic": "12",
    }
    parts = str(val).strip().split()
    if len(parts) == 3:
        dia, mes_str, anio = parts
        mes_num = meses_es.get(mes_str.lower()[:3])
        if mes_num:
            try:
                return datetime.strptime(f"{dia}/{mes_num}/{anio}", "%d/%m/%Y").date()
            except ValueError:
                pass
    return None


def _norm_invoice_key_part(value):
    value = "" if value is None else str(value)
    return value.strip().lower()


def _invoice_import_key(account_id, serie, folio, concepto, fecha_cobro, total):
    """Llave natural para hacer idempotente la carga CSV de cobros.

    Prioriza folio/serie cuando existen. Si el CSV no trae folio, usa una
    identidad derivada de cuenta + concepto + fecha + total.
    """
    folio_norm = _norm_invoice_key_part(folio)
    serie_norm = _norm_invoice_key_part(serie)
    if folio_norm:
        raw = "|".join(("folio", str(account_id), serie_norm, folio_norm))
    else:
        fecha_norm = fecha_cobro.isoformat() if fecha_cobro else ""
        total_norm = f"{float(total or 0):.2f}"
        raw = "|".join((
            "fallback",
            str(account_id),
            _norm_invoice_key_part(concepto),
            fecha_norm,
            total_norm,
        ))
    return "csv:" + hashlib.md5(raw.encode("utf-8")).hexdigest()


# Cache de presencia de la columna zoho_appointment_id en cs_appointments.
# Evita un round-trip a Supabase en cada carga de CSV.
_HAS_ZOHO_APPT_COL: bool | None = None


def _has_zoho_appointment_col() -> bool:
    global _HAS_ZOHO_APPT_COL
    if _HAS_ZOHO_APPT_COL is not None:
        return _HAS_ZOHO_APPT_COL
    try:
        from sqlalchemy import inspect as _sa_inspect
        cols = {c["name"] for c in _sa_inspect(db.engine).get_columns("cs_appointments")}
        _HAS_ZOHO_APPT_COL = "zoho_appointment_id" in cols
    except Exception:
        _HAS_ZOHO_APPT_COL = False
    return _HAS_ZOHO_APPT_COL


def _parse_datetime_citas(val):
    """Parsea '06/04/2026 17:41:21'."""
    if not val or val == "nan" or str(val).strip() == "":
        return None
    for fmt in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y"):
        try:
            return datetime.strptime(str(val).strip(), fmt)
        except ValueError:
            continue
    return None


def _match_account(cliente_nombre, accounts_map, client_id_map=None):
    """Busca la cuenta por client_id exacto o nombre parcial (case-insensitive)."""
    if not cliente_nombre:
        return None
    nombre_lower = str(cliente_nombre).strip().lower()
    # Primero intentar match exacto por client_id (AX-0001, etc.)
    if client_id_map:
        cid_upper = str(cliente_nombre).strip().upper()
        if cid_upper in client_id_map:
            return client_id_map[cid_upper]
    # Fallback: match por nombre
    for acc_nombre, acc_id in accounts_map.items():
        if acc_nombre.lower() in nombre_lower or nombre_lower in acc_nombre.lower():
            return acc_id
    return None


@cs_bp.route("/cargar-datos")
def cargar_datos():
    """Vista para cargar CSVs de cobros y citas."""
    import zoho_analytics
    accounts = CSAccount.query.filter(db.or_(CSAccount.en_due_diligence.is_(None), CSAccount.en_due_diligence.is_(False))).order_by(CSAccount.nombre).all()
    num_invoices = CSInvoice.query.count()
    num_appointments = CSAppointment.query.count()
    return render_template(
        "cs_cargar_datos.html",
        accounts=accounts, num_invoices=num_invoices,
        num_appointments=num_appointments,
        zoho_analytics_configured=zoho_analytics.is_configured(),
        zoho_view_id=zoho_analytics.VIEW_ID,
        **_ctx(),
    )


@cs_bp.route("/cargar-datos/plantilla-cobros")
def plantilla_cobros():
    """Descarga la plantilla CSV para cobros."""
    content = "ID,Cliente,Folio,Total,Pagado,Pendiente,Fecha de Cobro,Estatus,UN\n"
    return send_file(
        io.BytesIO(content.encode("utf-8-sig")),
        as_attachment=True,
        download_name="plantilla_cobros.csv",
        mimetype="text/csv",
    )


@cs_bp.route("/cargar-datos/plantilla-citas")
def plantilla_citas():
    """Descarga la plantilla CSV para citas/operación.
    'ID' = ID de la visita (no del cliente). 'Cliente' = nombre del cliente."""
    headers = "ID,Cliente,Propiedad,Dirección,Zona,Tecnico,Fecha de Inicio,Fecha de Terminación,Estatus,Titulo Servicio,Cantidad\n"
    ejemplo = "VIS-00123,Walmart Mexico,Sucursal Centro,Av. Reforma 100,CDMX-Centro,Juan Perez,06/04/2026 09:00:00,06/04/2026 11:00:00,Terminada,Servicio Aromatex,1\n"
    return send_file(
        io.BytesIO((headers + ejemplo).encode("utf-8-sig")),
        as_attachment=True,
        download_name="plantilla_citas.csv",
        mimetype="text/csv",
    )


@cs_bp.route("/cargar-datos/cobros", methods=["POST"])
@require_cs_admin
def cargar_cobros():
    """Procesa CSV de cobros/facturas."""
    from sqlalchemy import text as _text

    file = request.files.get("archivo")
    if not file or not (file.filename or "").lower().endswith(".csv"):
        flash("Subí un archivo .csv válido (cualquier mayúscula/minúscula).", "error")
        return redirect(url_for("cs.cargar_datos"))

    # Build account name + client_id maps
    accounts = CSAccount.query.filter(db.or_(CSAccount.en_due_diligence.is_(None), CSAccount.en_due_diligence.is_(False))).all()
    accounts_map = {a.nombre: str(a.id) for a in accounts}
    client_id_map = {a.client_id.upper(): str(a.id) for a in accounts if a.client_id}

    import logging
    logging.warning(f"[COBROS DEBUG] client_id_map keys (first 20): {list(client_id_map.keys())[:20]}")
    logging.warning(f"[COBROS DEBUG] accounts_map keys (first 20): {list(accounts_map.keys())[:20]}")

    content = file.read().decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(content))

    insertados = 0
    no_match = 0
    errores = 0
    batch = []
    debug_no_match = []
    debug_errors = []

    sql_upsert = _text("""
        INSERT INTO cs_invoices
            (account_id, cs_import_key, folio, serie, concepto, uen,
             subtotal, impuestos, total, pendiente, pagado,
             fecha_cobro, fecha_vencimiento, fecha_pago, estatus)
        VALUES
            (:account_id, :cs_import_key, :folio, :serie, :concepto, :uen,
             :subtotal, :impuestos, :total, :pendiente, :pagado,
             :fecha_cobro, :fecha_vencimiento, :fecha_pago, :estatus)
        ON CONFLICT (cs_import_key) WHERE cs_import_key IS NOT NULL
        DO UPDATE SET
            account_id = EXCLUDED.account_id,
            folio = EXCLUDED.folio,
            serie = EXCLUDED.serie,
            concepto = EXCLUDED.concepto,
            uen = EXCLUDED.uen,
            subtotal = EXCLUDED.subtotal,
            impuestos = EXCLUDED.impuestos,
            total = EXCLUDED.total,
            pendiente = EXCLUDED.pendiente,
            pagado = EXCLUDED.pagado,
            fecha_cobro = EXCLUDED.fecha_cobro,
            fecha_vencimiento = EXCLUDED.fecha_vencimiento,
            fecha_pago = EXCLUDED.fecha_pago,
            estatus = EXCLUDED.estatus
    """)

    def _flush_cobros(buf):
        if not buf:
            return 0, 0
        try:
            db.session.execute(sql_upsert, buf)
            db.session.commit()
            return len(buf), 0
        except Exception as e:
            db.session.rollback()
            if len(debug_errors) < 3:
                debug_errors.append(str(e)[:160])
            ok = fail = 0
            for item in buf:
                try:
                    db.session.execute(sql_upsert, item)
                    db.session.commit()
                    ok += 1
                except Exception as e2:
                    db.session.rollback()
                    fail += 1
                    if len(debug_errors) < 3:
                        debug_errors.append(str(e2)[:160])
            return ok, fail

    for row in reader:
        # Intentar por columna ID, luego Contrato, luego Cliente
        id_val = row.get("ID", "").strip()
        contrato = row.get("Contrato", "").strip()
        cliente = row.get("Cliente", "").strip()
        # Ignorar "-" como valor de ID
        if id_val == "-":
            id_val = ""
        acc_id = _match_account(id_val or contrato or cliente, accounts_map, client_id_map)
        if not acc_id:
            no_match += 1
            if len(debug_no_match) < 10:
                debug_no_match.append(f"ID='{id_val}' Contrato='{contrato}' Cliente='{cliente}'")
            continue

        try:
            serie = row.get("Serie de Folio", "")
            folio = row.get("Folio", "")
            concepto = row.get("Concepto", "")
            total = _parse_money(row.get("Total"))
            fecha_cobro = _parse_date_cobros(row.get("Fecha de Cobro"))
            batch.append({
                "account_id": acc_id,
                "cs_import_key": _invoice_import_key(
                    acc_id, serie, folio, concepto, fecha_cobro, total
                ),
                "folio": folio,
                "serie": serie,
                "concepto": concepto,
                "uen": row.get("UEN", "") or row.get("UN", ""),
                "subtotal": _parse_money(row.get("Monto Subtotal")),
                "impuestos": _parse_money(row.get("Impuestos")),
                "total": total,
                "pendiente": _parse_money(row.get("Pendiente")),
                "pagado": _parse_money(row.get("Pagado")),
                "fecha_cobro": fecha_cobro,
                "fecha_vencimiento": _parse_date_cobros(row.get("Fecha de Vencimiento")),
                "fecha_pago": _parse_date_cobros(row.get("Fecha de Pago")),
                "estatus": row.get("Estatus", ""),
            })
            if len(batch) >= 500:
                ok, fail = _flush_cobros(batch)
                insertados += ok
                errores += fail
                batch = []
        except Exception as e:
            errores += 1
            if len(debug_errors) < 3:
                debug_errors.append(str(e)[:160])

    if batch:
        ok, fail = _flush_cobros(batch)
        insertados += ok
        errores += fail

    _recalcular_facturacion(accounts)

    logging.warning(f"[COBROS DEBUG] no_match samples: {debug_no_match}")
    logging.warning(f"[COBROS DEBUG] CSV columns: {reader.fieldnames}")

    return render_template(
        "cs_cargar_resultado.html",
        tipo="Cobros", insertados=insertados, no_match=no_match,
        errores=errores, total=insertados + no_match + errores,
        debug_info=(
            f"upsert por cs_import_key | client_id_map keys: {list(client_id_map.keys())[:20]} "
            f"| no_match samples: {debug_no_match}"
            + (f" | errores: {debug_errors}" if debug_errors else "")
        ),
        **_ctx(),
    )


@cs_bp.route("/cargar-datos/citas", methods=["POST"])
@require_cs_admin
def cargar_citas():
    """Procesa CSV de citas/operación.

    Reglas:
    - 'Cliente' (nombre) → matchea cs_accounts (case-insensitive, parcial).
    - 'ID' del CSV es el ID de la VISITA (no del cliente) → se guarda como
      zoho_appointment_id para upsert idempotente (recargar el mismo CSV no
      duplica filas, actualiza por ID de visita).
    - Si la columna zoho_appointment_id no existe aún en la BD, cae a INSERT
      plano (posibles duplicados) y avisa al usuario en el resultado.
    """
    import logging as _logging
    from sqlalchemy import text as _text

    file = request.files.get("archivo")
    if not file or not (file.filename or "").lower().endswith(".csv"):
        flash("Subí un archivo .csv válido (cualquier mayúscula/minúscula). No se procesó nada.", "error")
        return redirect(url_for("cs.cargar_datos"))

    accounts = CSAccount.query.filter(db.or_(CSAccount.en_due_diligence.is_(None), CSAccount.en_due_diligence.is_(False))).all()
    accounts_map = {a.nombre: str(a.id) for a in accounts}

    try:
        content = file.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        flash("El archivo no es UTF-8. Guardalo como CSV UTF-8 desde Excel/Sheets y reintentá.", "error")
        return redirect(url_for("cs.cargar_datos"))

    reader = csv.DictReader(io.StringIO(content))
    columnas_detectadas = reader.fieldnames or []

    has_zoho_col = _has_zoho_appointment_col()

    insertados = 0
    no_match = 0
    errores = 0
    samples_no_match: list[str] = []
    samples_error: list[str] = []
    batch: list[dict] = []
    abort_upsert: bool = False  # si el primer batch falla con error estructural, bajamos a INSERT plano

    # SQL raw para esquivar problemas con SQLAlchemy Table.__table__ + .values(list).
    # El WHERE en ON CONFLICT matchea el índice único parcial
    # (ux_cs_appointments_zoho_id ... WHERE zoho_appointment_id IS NOT NULL).
    _SQL_UPSERT = _text("""
        INSERT INTO cs_appointments
            (account_id, propiedad, direccion, zona, tecnico,
             fecha_inicio, fecha_terminacion, estatus, titulo_servicio,
             cantidad, precio_unitario, zoho_appointment_id)
        VALUES
            (:account_id, :propiedad, :direccion, :zona, :tecnico,
             :fecha_inicio, :fecha_terminacion, :estatus, :titulo_servicio,
             :cantidad, :precio_unitario, :zoho_appointment_id)
        ON CONFLICT (zoho_appointment_id) WHERE zoho_appointment_id IS NOT NULL
        DO UPDATE SET
            account_id = EXCLUDED.account_id,
            propiedad = EXCLUDED.propiedad,
            direccion = EXCLUDED.direccion,
            zona = EXCLUDED.zona,
            tecnico = EXCLUDED.tecnico,
            fecha_inicio = EXCLUDED.fecha_inicio,
            fecha_terminacion = EXCLUDED.fecha_terminacion,
            estatus = EXCLUDED.estatus,
            titulo_servicio = EXCLUDED.titulo_servicio,
            cantidad = EXCLUDED.cantidad,
            precio_unitario = EXCLUDED.precio_unitario
    """)
    _SQL_INSERT = _text("""
        INSERT INTO cs_appointments
            (account_id, propiedad, direccion, zona, tecnico,
             fecha_inicio, fecha_terminacion, estatus, titulo_servicio,
             cantidad, precio_unitario)
        VALUES
            (:account_id, :propiedad, :direccion, :zona, :tecnico,
             :fecha_inicio, :fecha_terminacion, :estatus, :titulo_servicio,
             :cantidad, :precio_unitario)
    """)

    def _flush(buf: list[dict]) -> tuple[int, int]:
        """Devuelve (ok, fail). Usa upsert SQL raw para filas con zoho_appointment_id,
        INSERT plano para el resto. Si el primer batch upsert falla con error estructural
        (columna no existe), bajamos `abort_upsert=True` y todo va a INSERT plano."""
        nonlocal abort_upsert
        if not buf:
            return 0, 0

        if abort_upsert or not has_zoho_col:
            rows_con_id = []
            rows_sin_id = [{k: v for k, v in r.items() if k != "zoho_appointment_id"} for r in buf]
        else:
            rows_con_id = [r for r in buf if r.get("zoho_appointment_id")]
            rows_sin_id = [{k: v for k, v in r.items() if k != "zoho_appointment_id"}
                            for r in buf if not r.get("zoho_appointment_id")]
        ok, fail = 0, 0

        # Upsert (executemany)
        if rows_con_id:
            try:
                db.session.execute(_SQL_UPSERT, rows_con_id)
                db.session.commit()
                ok += len(rows_con_id)
            except Exception as e:
                db.session.rollback()
                msg = str(e)[:200]
                _logging.warning("[CITAS] upsert SQL falló — abortando upsert: %s", msg)
                samples_error.append(f"upsert: {msg}")
                abort_upsert = True
                # Reintentar este batch como INSERT plano
                fallback_rows = [{k: v for k, v in r.items() if k != "zoho_appointment_id"} for r in rows_con_id]
                try:
                    db.session.execute(_SQL_INSERT, fallback_rows)
                    db.session.commit()
                    ok += len(fallback_rows)
                except Exception as e2:
                    db.session.rollback()
                    fail += len(fallback_rows)
                    samples_error.append(f"insert fallback: {str(e2)[:120]}")

        # INSERT plano
        if rows_sin_id:
            try:
                db.session.execute(_SQL_INSERT, rows_sin_id)
                db.session.commit()
                ok += len(rows_sin_id)
            except Exception as e:
                db.session.rollback()
                _logging.warning("[CITAS] insert bulk falló — fila por fila: %s", str(e)[:150])
                for r in rows_sin_id:
                    try:
                        db.session.execute(_SQL_INSERT, r)
                        db.session.commit()
                        ok += 1
                    except Exception as e2:
                        db.session.rollback()
                        fail += 1
                        if len(samples_error) < 3:
                            samples_error.append(f"row: {str(e2)[:120]}")

        return ok, fail

    for row in reader:
        visita_id = (row.get("ID") or "").strip()  # ID de la visita, NO del cliente
        cliente = (row.get("Cliente") or "").strip()
        acc_id = _match_account(cliente, accounts_map) if cliente else None
        if not acc_id:
            no_match += 1
            if len(samples_no_match) < 5 and cliente:
                samples_no_match.append(cliente)
            continue

        try:
            precio_raw = row.get("Precio UNITARIO con Descuento") or row.get("Precio Unitario") or None
            precio = float(precio_raw) if precio_raw else None
            r_dict = {
                "account_id": acc_id,
                "propiedad": (row.get("Propiedad") or "").strip(),
                "direccion": (row.get("Dirección") or row.get("Direccion") or "").strip(),
                "zona": (row.get("Zona") or "").strip(),
                "tecnico": (row.get("Tecnico") or row.get("Técnico") or "").strip(),
                "fecha_inicio": _parse_datetime_citas(row.get("Fecha de Inicio")),
                "fecha_terminacion": _parse_datetime_citas(row.get("Fecha de Terminación") or row.get("Fecha de Terminacion") or ""),
                "estatus": (row.get("Estatus") or "").strip(),
                "titulo_servicio": (row.get("Titulo Servicio") or row.get("Título Servicio") or "").strip(),
                "cantidad": int(float(row.get("Cantidad") or 1)),
                "precio_unitario": precio,
            }
            if has_zoho_col and visita_id:
                r_dict["zoho_appointment_id"] = visita_id[:64]
            batch.append(r_dict)

            if len(batch) >= 500:
                ins, fail = _flush(batch)
                insertados += ins
                errores += fail
                batch = []
        except (ValueError, TypeError) as e:
            errores += 1
            if len(samples_error) < 3:
                samples_error.append(f"row parse: {str(e)[:120]}")

    if batch:
        ins, fail = _flush(batch)
        insertados += ins
        errores += fail

    debug_parts = [f"columnas detectadas: {columnas_detectadas}"]
    if not has_zoho_col:
        debug_parts.append("⚠️ Columna zoho_appointment_id NO existe — corré la migración en Supabase para evitar duplicados al reimportar")
    if samples_no_match:
        debug_parts.append(f"clientes sin match: {samples_no_match}")
    if samples_error:
        debug_parts.append(f"errores: {samples_error}")
    if insertados == 0 and (no_match + errores) > 0:
        debug_parts.insert(0, "⚠️ No se insertó ninguna fila — revisá headers o nombres de clientes.")

    return render_template(
        "cs_cargar_resultado.html",
        tipo="Citas", insertados=insertados, no_match=no_match,
        errores=errores, total=insertados + no_match + errores,
        debug_info=" | ".join(debug_parts),
        **_ctx(),
    )


# ──────────────────────────────────────────────
# Zoho Analytics → sync citas
# ──────────────────────────────────────────────

@cs_bp.route("/cargar-datos/zoho-sync", methods=["POST"])
@require_cs_admin
def zoho_sync_citas():
    """Jala citas directamente desde Zoho Analytics API y las upsertea."""
    import logging as _logging
    from sqlalchemy import text as _text
    from datetime import timedelta
    import zoho_analytics

    if not zoho_analytics.is_configured():
        flash("Zoho Analytics no configurado. Configura las variables de entorno.", "error")
        return redirect(url_for("cs.cargar_datos"))

    days = int(request.form.get("days", 30))
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    criteria = f'"Fecha de Inicio" >= \'{since}\''

    try:
        result = zoho_analytics.fetch_citas(criteria=criteria)
    except Exception as e:
        flash(f"Error conectando con Zoho Analytics: {e}", "error")
        return redirect(url_for("cs.cargar_datos"))

    if "error" in result:
        flash(f"Zoho Analytics: {result['error']}", "error")
        return redirect(url_for("cs.cargar_datos"))

    rows = result.get("rows", [])
    if not rows:
        flash("Zoho Analytics devolvió 0 filas.", "error")
        return redirect(url_for("cs.cargar_datos"))

    accounts = CSAccount.query.filter(db.or_(CSAccount.en_due_diligence.is_(None), CSAccount.en_due_diligence.is_(False))).all()
    accounts_map = {a.nombre: str(a.id) for a in accounts}
    has_zoho_col = _has_zoho_appointment_col()

    insertados = 0
    no_match = 0
    errores = 0
    samples_no_match: list[str] = []
    samples_error: list[str] = []
    batch: list[dict] = []

    _SQL_UPSERT = _text("""
        INSERT INTO cs_appointments
            (account_id, propiedad, direccion, zona, tecnico,
             fecha_inicio, fecha_terminacion, estatus, titulo_servicio,
             cantidad, zoho_appointment_id)
        VALUES
            (:account_id, :propiedad, :direccion, :zona, :tecnico,
             :fecha_inicio, :fecha_terminacion, :estatus, :titulo_servicio,
             :cantidad, :zoho_appointment_id)
        ON CONFLICT (zoho_appointment_id) WHERE zoho_appointment_id IS NOT NULL
        DO UPDATE SET
            account_id = EXCLUDED.account_id,
            propiedad = EXCLUDED.propiedad,
            direccion = EXCLUDED.direccion,
            zona = EXCLUDED.zona,
            tecnico = EXCLUDED.tecnico,
            fecha_inicio = EXCLUDED.fecha_inicio,
            fecha_terminacion = EXCLUDED.fecha_terminacion,
            estatus = EXCLUDED.estatus,
            titulo_servicio = EXCLUDED.titulo_servicio,
            cantidad = EXCLUDED.cantidad
    """)
    _SQL_INSERT = _text("""
        INSERT INTO cs_appointments
            (account_id, propiedad, direccion, zona, tecnico,
             fecha_inicio, fecha_terminacion, estatus, titulo_servicio, cantidad)
        VALUES
            (:account_id, :propiedad, :direccion, :zona, :tecnico,
             :fecha_inicio, :fecha_terminacion, :estatus, :titulo_servicio, :cantidad)
    """)

    def _flush_zoho(buf):
        if not buf:
            return 0, 0
        if has_zoho_col:
            try:
                db.session.execute(_SQL_UPSERT, buf)
                db.session.commit()
                return len(buf), 0
            except Exception as e:
                db.session.rollback()
                _logging.warning("[ZOHO-CITAS] upsert batch falló: %s", str(e)[:200])
        plain = [{k: v for k, v in r.items() if k != "zoho_appointment_id"} for r in buf]
        try:
            db.session.execute(_SQL_INSERT, plain)
            db.session.commit()
            return len(plain), 0
        except Exception as e:
            db.session.rollback()
            _logging.warning("[ZOHO-CITAS] insert batch falló: %s", str(e)[:200])
            return 0, len(plain)

    for row in rows:
        visita_id = str(row.get("ID", "")).strip()
        cliente = str(row.get("Cliente", "")).strip()
        acc_id = _match_account(cliente, accounts_map) if cliente else None
        if not acc_id:
            no_match += 1
            if len(samples_no_match) < 5 and cliente:
                samples_no_match.append(cliente)
            continue

        try:
            r_dict = {
                "account_id": acc_id,
                "propiedad": str(row.get("Propiedad", "")).strip(),
                "direccion": str(row.get("Dirección", "") or row.get("Direccion", "")).strip(),
                "zona": str(row.get("Zona", "")).strip(),
                "tecnico": str(row.get("Tecnico", "") or row.get("Técnico", "")).strip(),
                "fecha_inicio": _parse_datetime_citas(row.get("Fecha de Inicio")),
                "fecha_terminacion": _parse_datetime_citas(
                    row.get("Fecha de Terminación") or row.get("Fecha de Terminacion") or ""
                ),
                "estatus": str(row.get("Estatus", "")).strip(),
                "titulo_servicio": str(
                    row.get("Titulo Servicio", "") or row.get("Título Servicio", "")
                ).strip(),
                "cantidad": int(float(row.get("Cantidad") or 1)),
            }
            if has_zoho_col and visita_id:
                r_dict["zoho_appointment_id"] = visita_id[:64]
            batch.append(r_dict)

            if len(batch) >= 2000:
                ins, fail = _flush_zoho(batch)
                insertados += ins
                errores += fail
                batch = []
        except (ValueError, TypeError) as e:
            errores += 1
            if len(samples_error) < 3:
                samples_error.append(f"row parse: {str(e)[:120]}")

    if batch:
        ins, fail = _flush_zoho(batch)
        insertados += ins
        errores += fail

    debug_parts = [f"Zoho Analytics: {result.get('count', 0)} filas recibidas"]
    if samples_no_match:
        debug_parts.append(f"clientes sin match: {samples_no_match}")
    if samples_error:
        debug_parts.append(f"errores: {samples_error}")

    return render_template(
        "cs_cargar_resultado.html",
        tipo="Citas (Zoho Analytics)", insertados=insertados, no_match=no_match,
        errores=errores, total=insertados + no_match + errores,
        debug_info=" | ".join(debug_parts),
        **_ctx(),
    )


@cs_bp.route("/api/zoho-analytics/workspaces")
def zoho_analytics_workspaces():
    """Discovery: lista workspaces disponibles en Zoho Analytics."""
    import zoho_analytics
    try:
        return jsonify(zoho_analytics.get_workspaces())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@cs_bp.route("/api/zoho-analytics/views/<workspace_id>")
def zoho_analytics_views(workspace_id):
    """Discovery: lista vistas/tablas de un workspace."""
    import zoho_analytics
    try:
        return jsonify(zoho_analytics.get_views(workspace_id))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ──────────────────────────────────────────────
# CRUD de citas por cuenta — para que el KAM gestione su plantilla activa
# sin depender de la carga masiva (que arrastra cancelaciones).
# ──────────────────────────────────────────────

def _parse_dt_iso(s):
    if not s:
        return None
    s = s.strip()
    if not s:
        return None
    try:
        # Acepta "YYYY-MM-DD" o "YYYY-MM-DDTHH:MM" del input HTML
        from datetime import datetime as _dt
        if "T" in s:
            return _dt.fromisoformat(s)
        return _dt.strptime(s[:10], "%Y-%m-%d")
    except (ValueError, TypeError):
        return None


@cs_bp.route("/api/cuentas/<uuid:account_id>/citas", methods=["GET"])
@require_cs_account_access
def listar_citas_cuenta(account_id):
    """GET /cs/api/cuentas/<id>/citas?solo_activas=1 — citas filtradas.
    Default: solo_activas=1 (oculta Cancelada / No Realizada / Archivada)."""
    account = _get_cs_account(account_id)

    solo_activas = request.args.get("solo_activas", "1") in ("1", "true", "yes")
    q = CSAppointment.query.filter(CSAppointment.account_id == account.id)
    if solo_activas:
        q = q.filter(~CSAppointment.estatus.in_(("Cancelada", "No Realizada", "Archivada")))
    rows = q.order_by(CSAppointment.fecha_inicio.desc().nullslast()).all()
    return jsonify([{
        "id": str(a.id), "propiedad": a.propiedad, "direccion": a.direccion,
        "zona": a.zona, "tecnico": a.tecnico,
        "fecha_inicio": a.fecha_inicio.isoformat() if a.fecha_inicio else None,
        "fecha_terminacion": a.fecha_terminacion.isoformat() if a.fecha_terminacion else None,
        "estatus": a.estatus, "titulo_servicio": a.titulo_servicio,
        "cantidad": a.cantidad,
    } for a in rows])


@cs_bp.route("/api/cuentas/<uuid:account_id>/citas", methods=["POST"])
@require_cs_account_access
def crear_cita(account_id):
    """KAM agrega una cita a la plantilla activa. Default estatus 'Agendada'."""
    account = _get_cs_account(account_id)

    data = request.get_json() or {}
    cita = CSAppointment(
        account_id=account.id,
        propiedad=data.get("propiedad", ""),
        direccion=data.get("direccion", ""),
        zona=data.get("zona", ""),
        tecnico=data.get("tecnico", ""),
        fecha_inicio=_parse_dt_iso(data.get("fecha_inicio")),
        fecha_terminacion=_parse_dt_iso(data.get("fecha_terminacion")),
        estatus=data.get("estatus") or "Agendada",
        titulo_servicio=data.get("titulo_servicio", ""),
        cantidad=int(data.get("cantidad") or 1),
    )
    db.session.add(cita)
    db.session.commit()
    return jsonify({"ok": True, "id": str(cita.id), "estatus": cita.estatus}), 201


@cs_bp.route("/api/citas/<uuid:cita_id>", methods=["PATCH"])
def actualizar_cita(cita_id):
    """KAM edita una cita. Permite cambiar cualquier campo, incluido estatus."""
    cita = db.session.get(CSAppointment, cita_id)
    if not cita:
        return jsonify({"error": "Cita no encontrada"}), 404
    account = _get_cs_account(cita.account_id)
    err = _require_account_access(account, json_response=True)
    if err:
        return err

    data = request.get_json() or {}
    for fld in ("propiedad", "direccion", "zona", "tecnico", "estatus", "titulo_servicio"):
        if fld in data:
            setattr(cita, fld, data[fld] or "")
    if "fecha_inicio" in data:
        cita.fecha_inicio = _parse_dt_iso(data["fecha_inicio"])
    if "fecha_terminacion" in data:
        cita.fecha_terminacion = _parse_dt_iso(data["fecha_terminacion"])
    if "cantidad" in data:
        try:
            cita.cantidad = int(data["cantidad"])
        except (ValueError, TypeError):
            pass
    db.session.commit()
    return jsonify({"ok": True, "id": str(cita.id), "estatus": cita.estatus})


@cs_bp.route("/api/citas/<uuid:cita_id>/cancelar", methods=["POST"])
def cancelar_cita(cita_id):
    """Soft-delete: marca estatus='Cancelada' en lugar de borrar.
    Preserva el historial — útil para auditoría."""
    cita = db.session.get(CSAppointment, cita_id)
    if not cita:
        return jsonify({"error": "Cita no encontrada"}), 404
    account = _get_cs_account(cita.account_id)
    err = _require_account_access(account, json_response=True)
    if err:
        return err
    cita.estatus = "Cancelada"
    db.session.commit()
    return jsonify({"ok": True})


@cs_bp.route("/api/citas/<uuid:cita_id>", methods=["DELETE"])
@require_cs_admin
def eliminar_cita(cita_id):
    """Hard delete: solo super_admin / director. KAMs usan /cancelar."""
    cita = db.session.get(CSAppointment, cita_id)
    if not cita:
        return jsonify({"error": "Cita no encontrada"}), 404
    db.session.delete(cita)
    db.session.commit()
    return jsonify({"ok": True})


@cs_bp.route("/cargar-datos/limpiar/<tipo>", methods=["POST"])
@require_cs_full_access
def limpiar_datos(tipo):
    """Elimina todos los registros de un tipo para recargar."""
    if tipo == "cobros":
        CSInvoice.query.delete()
        db.session.commit()
    elif tipo == "citas":
        CSAppointment.query.delete()
        db.session.commit()
    return redirect(url_for("cs.cargar_datos"))


def _recalcular_facturacion(accounts):
    """Recalcula los campos facturacion_q1, pagado_q1, pendiente_q1 desde las facturas."""
    for acc in accounts:
        totals = db.session.query(
            func.coalesce(func.sum(CSInvoice.total), 0),
            func.coalesce(func.sum(CSInvoice.pagado), 0),
            func.coalesce(func.sum(CSInvoice.pendiente), 0),
            func.count(CSInvoice.id),
        ).filter_by(account_id=acc.id).first()

        acc.facturacion_q1 = float(totals[0])
        acc.pagado_q1 = float(totals[1])
        acc.pendiente_q1 = float(totals[2])
        acc.num_facturas_q1 = totals[3]
    db.session.commit()


# ══════════════════════════════════════════════
# CONTACTOS — directorio global + CRUD por cuenta
# ══════════════════════════════════════════════
@cs_bp.route("/contactos")
def contactos_directory():
    """Directorio global de contactos."""
    q = CSContacto.query
    buscar = request.args.get("q", "").strip()
    if buscar:
        q = q.filter(
            db.or_(
                CSContacto.nombre.ilike(f"%{buscar}%"),
                CSContacto.correo.ilike(f"%{buscar}%"),
                CSContacto.puesto.ilike(f"%{buscar}%"),
            )
        )
    contactos = q.order_by(CSContacto.is_owner.desc(), CSContacto.nombre).all()
    accounts = CSAccount.query.filter(db.or_(CSAccount.en_due_diligence.is_(None), CSAccount.en_due_diligence.is_(False))).order_by(CSAccount.nombre).all()
    return render_template(
        "cs_contactos.html",
        contactos=contactos, accounts=accounts, buscar=buscar, **_ctx(),
    )


@cs_bp.route("/contactos/crear", methods=["POST"])
def crear_contacto():
    account_id = request.form.get("account_id", "").strip()
    if not account_id:
        return redirect(url_for("cs.contactos_directory"))
    account = _get_cs_account(account_id)
    err = _require_account_access(account)
    if err:
        return err
    contacto = CSContacto(
        account_id=account_id,
        nombre=request.form.get("nombre", "").strip(),
        puesto=request.form.get("puesto", "").strip(),
        telefono=request.form.get("telefono", "").strip(),
        correo=request.form.get("correo", "").strip(),
        is_owner=request.form.get("is_owner") == "on",
        notas=request.form.get("notas", "").strip(),
    )
    db.session.add(contacto)
    db.session.commit()
    # Redirect back to where they came from
    referer = request.form.get("redirect", "")
    if referer:
        return redirect(referer)
    return redirect(url_for("cs.contactos_directory"))


@cs_bp.route("/contactos/<uuid:contacto_id>/editar", methods=["POST"])
def editar_contacto(contacto_id):
    c = db.session.get(CSContacto, contacto_id)
    if not c:
        return "No encontrado", 404
    account = _get_cs_account(c.account_id)
    err = _require_account_access(account)
    if err:
        return err
    c.nombre = request.form.get("nombre", c.nombre).strip()
    c.puesto = request.form.get("puesto", c.puesto).strip()
    c.telefono = request.form.get("telefono", c.telefono).strip()
    c.correo = request.form.get("correo", c.correo).strip()
    c.is_owner = request.form.get("is_owner") == "on"
    c.notas = request.form.get("notas", c.notas).strip()
    db.session.commit()
    referer = request.form.get("redirect", "")
    if referer:
        return redirect(referer)
    return redirect(url_for("cs.contactos_directory"))


@cs_bp.route("/contactos/<uuid:contacto_id>/delete", methods=["POST"])
def eliminar_contacto(contacto_id):
    c = db.session.get(CSContacto, contacto_id)
    if c:
        account = _get_cs_account(c.account_id)
        err = _require_account_access(account)
        if err:
            return err
        db.session.delete(c)
        db.session.commit()
    referer = request.form.get("redirect", "")
    if referer:
        return redirect(referer)
    return redirect(url_for("cs.contactos_directory"))


# ══════════════════════════════════════════════
# INCIDENCIAS — Registro de incidencias por cuenta
# ══════════════════════════════════════════════
@cs_bp.route("/account/<uuid:account_id>/incidencias", methods=["POST"])
@require_cs_account_access
def crear_incidencia(account_id):
    tipo = request.form.get("tipo", "").strip()
    if not tipo:
        return redirect(url_for("cs.account_detail", account_id=account_id) + "?tab=incidencias")

    propiedad_id = request.form.get("propiedad_id", "").strip() or None
    propiedad_nombre = ""
    if propiedad_id:
        prop = db.session.get(CSPropiedad, propiedad_id)
        if prop and str(prop.account_id) != str(account_id):
            return _permission_denied()
        propiedad_nombre = prop.nombre if prop else ""

    fecha_str = request.form.get("fecha_incidencia", "").strip()
    fecha_inc = None
    if fecha_str:
        try:
            fecha_inc = datetime.strptime(fecha_str, "%Y-%m-%d").date()
        except ValueError:
            pass

    fecha_comp_str = request.form.get("fecha_compromiso", "").strip()
    fecha_comp = None
    if fecha_comp_str:
        try:
            fecha_comp = datetime.strptime(fecha_comp_str, "%Y-%m-%d").date()
        except ValueError:
            pass

    inc = CSIncidencia(
        account_id=account_id,
        propiedad_id=propiedad_id,
        propiedad_nombre=propiedad_nombre,
        servicio=request.form.get("servicio", "Aroma"),
        tipo=tipo,
        detalle=request.form.get("detalle", "").strip(),
        status="Abierta",
        zona=request.form.get("zona", "").strip(),
        quien_reporta=request.form.get("quien_reporta", "").strip(),
        contacto_cliente=request.form.get("contacto_cliente", "").strip(),
        responsable=request.form.get("responsable", "").strip(),
        fecha_incidencia=fecha_inc or date.today(),
        fecha_compromiso=fecha_comp,
        created_by=session.get("user_nombre", ""),
    )
    db.session.add(inc)
    db.session.commit()
    return redirect(url_for("cs.account_detail", account_id=account_id) + "?tab=incidencias")


@cs_bp.route("/account/<uuid:account_id>/incidencias/<uuid:inc_id>/status", methods=["POST"])
@require_cs_account_access
def cambiar_status_incidencia(account_id, inc_id):
    inc = db.session.get(CSIncidencia, inc_id)
    if inc:
        if str(inc.account_id) != str(account_id):
            return _permission_denied()
        account = _get_cs_account(inc.account_id)
        err = _require_account_access(account)
        if err:
            return err
        nuevo = request.form.get("status", "")
        if nuevo in ("Abierta", "En proceso", "Resuelta"):
            inc.status = nuevo
            if nuevo == "Resuelta" and not inc.fecha_solucion:
                inc.fecha_solucion = date.today()
                if inc.fecha_incidencia:
                    inc.tiempo_respuesta = (date.today() - inc.fecha_incidencia).days
        comentario = request.form.get("comentarios", "").strip()
        if comentario:
            inc.comentarios_operaciones = comentario
        db.session.commit()
    return redirect(url_for("cs.account_detail", account_id=account_id) + "?tab=incidencias")


@cs_bp.route("/api/propiedades/<uuid:account_id>")
@require_cs_account_access
def api_propiedades(account_id):
    """API JSON de propiedades por cuenta (para search dinámico)."""
    q = request.args.get("q", "").strip()
    query = CSPropiedad.query.filter_by(account_id=account_id)
    if q:
        query = query.filter(CSPropiedad.nombre.ilike(f"%{q}%"))
    props = query.order_by(CSPropiedad.nombre).limit(50).all()
    return jsonify([{
        "id": str(p.id), "nombre": p.nombre,
        "zona": p.zona, "unidad_negocio": p.unidad_negocio,
    } for p in props])


# ──────────────────────────────────────────────
# Portafolio de sucursales — CSV upload por cuenta
# Cross-reference con CSAppointment.propiedad por nombre (case-insensitive)
# ──────────────────────────────────────────────
@cs_bp.route("/cuentas/<uuid:account_id>/propiedades/template-csv")
@require_cs_account_access
def descargar_template_propiedades(account_id):
    """Plantilla CSV con headers + 2 filas de ejemplo."""
    acc = _get_cs_account(account_id)
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["nombre", "direccion", "zona", "unidad_negocio"])
    w.writerow(["Sucursal Centro", "Av. Reforma 100, CDMX", "CDMX-Centro", "AROMATEX"])
    w.writerow(["Sucursal Sur", "Calz. de Tlalpan 500, CDMX", "CDMX-Sur", "PESTEX"])
    bom = "﻿" + out.getvalue()
    nombre_archivo = f"plantilla_sucursales_{(acc.nombre if acc else 'cuenta').replace(' ', '_')[:40]}.csv"
    return send_file(
        io.BytesIO(bom.encode("utf-8")),
        mimetype="text/csv",
        as_attachment=True,
        download_name=nombre_archivo,
    )


@cs_bp.route("/cuentas/<uuid:account_id>/propiedades/upload-csv", methods=["POST"])
@require_cs_account_access
def upload_propiedades_csv(account_id):
    """Procesa CSV de sucursales y upserta en cs_propiedades.
    Después calcula cross-ref con citas existentes (match por nombre, ci)."""
    acc = _get_cs_account(account_id)

    file = request.files.get("archivo")
    if not file or not (file.filename or "").lower().endswith(".csv"):
        flash("Subí un archivo .csv válido", "error")
        return redirect(url_for("cs.clientes"))

    content = file.read().decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(content))

    existentes = {p.nombre.strip().lower(): p for p in CSPropiedad.query.filter_by(account_id=account_id).all()}
    creadas = 0
    actualizadas = 0
    errores = 0

    for row in reader:
        nombre = (row.get("nombre") or row.get("Nombre") or row.get("propiedad") or row.get("Propiedad") or "").strip()
        if not nombre:
            errores += 1
            continue
        direccion = (row.get("direccion") or row.get("Direccion") or row.get("dirección") or row.get("Dirección") or "").strip()
        zona = (row.get("zona") or row.get("Zona") or "").strip()
        un = (row.get("unidad_negocio") or row.get("UnidadNegocio") or row.get("Unidad") or row.get("UN") or "").strip().upper()

        key = nombre.lower()
        if key in existentes:
            p = existentes[key]
            if direccion:
                p.direccion = direccion
            if zona:
                p.zona = zona
            if un:
                p.unidad_negocio = un
            actualizadas += 1
        else:
            p = CSPropiedad(
                account_id=account_id, nombre=nombre,
                direccion=direccion, zona=zona, unidad_negocio=un,
            )
            db.session.add(p)
            existentes[key] = p
            creadas += 1

    db.session.commit()

    # Cross-reference: cuántas citas existentes matchean por nombre (case-insensitive)
    nombres_canonicos = list(existentes.keys())
    matched = 0
    citas_total = CSAppointment.query.filter_by(account_id=account_id).count()
    if nombres_canonicos:
        matched = (
            db.session.query(func.count(CSAppointment.id))
            .filter(CSAppointment.account_id == account_id)
            .filter(func.lower(func.trim(CSAppointment.propiedad)).in_(nombres_canonicos))
            .scalar() or 0
        )

    sucursales_unicas_en_citas = (
        db.session.query(func.count(func.distinct(func.lower(func.trim(CSAppointment.propiedad)))))
        .filter(CSAppointment.account_id == account_id)
        .filter(CSAppointment.propiedad != "")
        .scalar() or 0
    )

    return render_template(
        "cs_cargar_resultado.html",
        tipo=f"Sucursales de {acc.nombre}",
        insertados=creadas + actualizadas,
        no_match=errores,
        errores=0,
        total=creadas + actualizadas + errores,
        extra_info={
            "creadas": creadas,
            "actualizadas": actualizadas,
            "total_portafolio": len(existentes),
            "citas_total": citas_total,
            "citas_matched": matched,
            "citas_unmatched": citas_total - matched,
            "sucursales_distintas_en_citas": sucursales_unicas_en_citas,
        },
        back_url=url_for("cs.clientes"),
        **_ctx(),
    )


# ══════════════════════════════════════════════
# ENTREGABLES — Flujo de servicio por cuenta
# ══════════════════════════════════════════════
@cs_bp.route("/account/<uuid:account_id>/entregables", methods=["POST"])
@require_cs_account_access
def crear_entregable(account_id):
    descripcion = request.form.get("descripcion", "").strip()
    if descripcion:
        # Calcular orden (siguiente en la UN)
        un = request.form.get("unidad_negocio", "").strip()
        max_orden = db.session.query(func.coalesce(func.max(CSEntregable.orden), 0)).filter_by(
            account_id=account_id, unidad_negocio=un
        ).scalar()
        adj = _parse_adjuntos(request.form)
        db.session.add(CSEntregable(
            account_id=account_id,
            unidad_negocio=un,
            descripcion=descripcion,
            fecha_entrega=request.form.get("fecha_entrega", "").strip(),
            responsable=request.form.get("responsable", "").strip(),
            orden=max_orden + 1, adjuntos=adj,
        ))
        db.session.commit()
    return redirect(url_for("cs.account_detail", account_id=account_id) + "?tab=entregables#entregables")


@cs_bp.route("/account/<uuid:account_id>/entregables/<uuid:ent_id>/delete", methods=["POST"])
@require_cs_account_access
def eliminar_entregable(account_id, ent_id):
    e = db.session.get(CSEntregable, ent_id)
    if e:
        if str(e.account_id) != str(account_id):
            return _permission_denied()
        account = _get_cs_account(e.account_id)
        err = _require_account_access(account)
        if err:
            return err
        db.session.delete(e)
        db.session.commit()
    return redirect(url_for("cs.account_detail", account_id=account_id) + "?tab=entregables#entregables")


# ══════════════════════════════════════════════
# ENCUESTA OPERATIVA KAM — Respuestas cerradas por cuenta
# ══════════════════════════════════════════════
@cs_bp.route("/account/<uuid:account_id>/workload-survey", methods=["POST"])
@require_cs_account_access
def guardar_workload_survey(account_id):
    account = _get_cs_account(account_id)

    periodo = request.form.get("periodo", "").strip() or date.today().strftime("%Y-%m")
    survey = CSWorkloadSurvey.query.filter_by(account_id=account.id, periodo=periodo).first()
    if not survey:
        survey = CSWorkloadSurvey(account_id=account.id, periodo=periodo)
        db.session.add(survey)

    activity_names = request.form.getlist("actividad")
    activity_hours = request.form.getlist("actividad_horas")
    actividades_horas = []
    for idx, actividad in enumerate(activity_names):
        actividad = (actividad or "").strip()
        if not actividad:
            continue
        horas = activity_hours[idx] if idx < len(activity_hours) else "No aplica"
        if horas not in WORKLOAD_ACTIVITY_HOUR_OPTIONS:
            horas = "No aplica"
        actividades_horas.append({"actividad": actividad[:200], "horas": horas})

    survey.kam_id = account.kam_id
    survey.horas_cliente = request.form.get("horas_cliente", "")
    survey.carga_esperada = request.form.get("carga_esperada", "")
    survey.motivo_carga = request.form.get("motivo_carga", "")
    survey.actividades_horas = actividades_horas
    survey.entregables_count = request.form.get("entregables_count", "")
    entregables_tipos = request.form.getlist("entregables_tipos")
    entregable_otro = (request.form.get("entregable_otro") or "").strip()
    if entregable_otro:
        entregables_tipos = [t for t in entregables_tipos if t != "Otro operativo"]
        entregables_tipos.append(f"Otro: {entregable_otro[:120]}")
    survey.entregables_tipos = entregables_tipos
    survey.frecuencia_entregable = request.form.get("frecuencia_entregable", "")
    survey.horas_entregables = request.form.get("horas_entregables", "")
    survey.dependencia_externa = request.form.get("dependencia_externa", "")
    survey.bloqueos_nivel = request.form.get("bloqueos_nivel", "")
    tipo_bloqueo = request.form.get("tipo_bloqueo", "")
    bloqueo_otro = (request.form.get("bloqueo_otro") or "").strip()
    if tipo_bloqueo == "Otro operativo" and bloqueo_otro:
        tipo_bloqueo = f"Otro: {bloqueo_otro[:120]}"
    survey.tipo_bloqueo = tipo_bloqueo
    survey.horas_bloqueos = request.form.get("horas_bloqueos", "")
    survey.recurrencia_bloqueo = request.form.get("recurrencia_bloqueo", "")
    survey.reprogramaciones_count = request.form.get("reprogramaciones_count", "")
    survey.incidencias_count = request.form.get("incidencias_count", "")
    survey.horas_incidencias = request.form.get("horas_incidencias", "")
    survey.causa_incidencias = request.form.get("causa_incidencias", "")

    db.session.commit()
    return redirect(url_for("cs.account_detail", account_id=account_id) + f"?periodo={periodo}&tab=workload-survey#workload-survey")


# ══════════════════════════════════════════════
# QBR
# ══════════════════════════════════════════════
@cs_bp.route("/account/<uuid:account_id>/qbr")
@require_cs_account_access
def download_qbr(account_id):
    """Descarga el QBR del trimestre actual por defecto.
    Override con ?q=Q1+2026 (o Q2/Q3/Q4) para trimestres pasados.
    FIX-2026-06-26: antes el trimestre estaba hardcoded en 'Q1 2026'."""
    account = _get_cs_account(account_id)
    if not account:
        return "Cuenta no encontrada", 404
    from cs_qbr_generator import generar_qbr, _parse_trim

    # Si no viene en query string, _parse_trim hace fallback al trimestre actual
    trimestre_param = request.args.get("q") or request.args.get("trimestre")
    _, _, q_label, trim_full = _parse_trim(trimestre_param)
    safe_q = q_label  # ej. "Q2"
    safe_year = trim_full.split()[-1]

    excel_buffer = generar_qbr(account, trimestre=trim_full)
    nombre_limpio = account.nombre.replace(" ", "_").replace("/", "-")
    return send_file(
        excel_buffer, as_attachment=True,
        download_name=f"QBR_{safe_q}_{safe_year}_{nombre_limpio}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


# ══════════════════════════════════════════════
# CRUD — KPIs, Notas, Tareas
# ══════════════════════════════════════════════
@cs_bp.route("/account/<uuid:account_id>/kpis", methods=["POST"])
@require_cs_account_access
def update_kpis(account_id):
    account = _get_cs_account(account_id)
    data = request.form
    nps_val = data.get("nps", "").strip()
    account.nps = float(nps_val) if nps_val else None
    pulso_val = data.get("pulso", "").strip()
    account.pulso = pulso_val if pulso_val in ("Sana", "Atención", "Riesgo") else None
    ef_val = data.get("eficiencia_operativa", "").strip()
    account.eficiencia_operativa = float(ef_val) if ef_val else None
    db.session.commit()
    return redirect(url_for("cs.account_detail", account_id=account_id))


@cs_bp.route("/account/<uuid:account_id>/notes", methods=["POST"])
@require_cs_account_access
def create_note(account_id):
    contenido = request.form.get("contenido", "").strip()
    autor = request.form.get("autor", "").strip()
    if contenido:
        adj = _parse_adjuntos(request.form)
        db.session.add(CSNote(account_id=account_id, autor=autor, contenido=contenido, adjuntos=adj))
        db.session.commit()
    return redirect(url_for("cs.account_detail", account_id=account_id) + "?tab=notas")


@cs_bp.route("/account/<uuid:account_id>/notes/<uuid:note_id>/delete", methods=["POST"])
@require_cs_account_access
def delete_note(account_id, note_id):
    note = db.session.get(CSNote, note_id)
    if note:
        if str(note.account_id) != str(account_id):
            return _permission_denied()
        account = _get_cs_account(note.account_id)
        err = _require_account_access(account)
        if err:
            return err
        db.session.delete(note)
        db.session.commit()
    return redirect(url_for("cs.account_detail", account_id=account_id) + "?tab=notas")


@cs_bp.route("/account/<uuid:account_id>/tasks", methods=["POST"])
@require_cs_account_access
def create_task(account_id):
    descripcion = request.form.get("descripcion", "").strip()
    if descripcion:
        fecha_str = request.form.get("fecha_limite", "").strip()
        fecha_limite = None
        if fecha_str:
            try:
                fecha_limite = datetime.strptime(fecha_str, "%Y-%m-%d").date()
            except ValueError:
                pass
        adj = _parse_adjuntos(request.form)
        db.session.add(CSTask(
            account_id=account_id, tipo=request.form.get("tipo", "check-in"),
            descripcion=descripcion, responsable=request.form.get("responsable", ""),
            fecha_limite=fecha_limite, adjuntos=adj,
        ))
        db.session.commit()
    return redirect(url_for("cs.account_detail", account_id=account_id) + "?tab=tareas")


@cs_bp.route("/account/<uuid:account_id>/tasks/<uuid:task_id>/toggle", methods=["POST"])
@require_cs_account_access
def toggle_task(account_id, task_id):
    task = db.session.get(CSTask, task_id)
    if task:
        if str(task.account_id) != str(account_id):
            return _permission_denied()
        account = _get_cs_account(task.account_id)
        err = _require_account_access(account)
        if err:
            return err
        task.completada = not task.completada
        db.session.commit()
    return redirect(url_for("cs.account_detail", account_id=account_id) + "?tab=tareas")


@cs_bp.route("/account/<uuid:account_id>/tasks/<uuid:task_id>/delete", methods=["POST"])
@require_cs_account_access
def delete_task(account_id, task_id):
    task = db.session.get(CSTask, task_id)
    if task:
        if str(task.account_id) != str(account_id):
            return _permission_denied()
        account = _get_cs_account(task.account_id)
        err = _require_account_access(account)
        if err:
            return err
        db.session.delete(task)
        db.session.commit()
    return redirect(url_for("cs.account_detail", account_id=account_id) + "?tab=tareas")

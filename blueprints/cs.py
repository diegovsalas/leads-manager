# blueprints/cs.py
"""
CS Dashboard — Customer Success para KAMs.
Rutas bajo /cs/
"""
import csv
import io
from datetime import datetime, date
from flask import Blueprint, render_template, request, redirect, url_for, session, jsonify, send_file, flash
from sqlalchemy import func
from extensions import db
from models import (
    CSAccount, CSInvoice, CSAppointment, CSNote, CSTask,
    CSOnboardingAccount, CSOpportunity, CSContacto, CSEntregable,
    CSEncuesta, CSIncidencia, CSPropiedad, UserCRM, RolCRM,
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
    accounts = q.all()
    account_ids = [a.id for a in accounts]

    # Facturación dinámica del periodo
    fact_periodo = _calc_facturacion_periodo(account_ids, inicio, fin)

    scores_map = calcular_health_scores_batch(accounts)

    mrr_total = sum(float(a.mrr or 0) for a in accounts)
    arr_total = sum(float(a.arr_proyectado or 0) for a in accounts)
    total_sucursales = sum(a.sucursales for a in accounts)

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
    account_scores.sort(key=lambda x: x["health"]["score"])
    top_riesgo = account_scores[:5]

    cat_counts = {"Sana": 0, "Atención": 0, "Riesgo": 0}
    for item in account_scores:
        cat_counts[item["health"]["categoria"]] += 1

    kams = _get_kams()
    kam_data = []
    for k in kams:
        accs_kam = [a for a in accounts if str(a.kam_id) == str(k.id)]
        kam_data.append({
            "id": str(k.id), "nombre": k.nombre,
            "num_cuentas": len(accs_kam),
            "mrr": sum(float(a.mrr or 0) for a in accs_kam),
            "sucursales": sum(a.sucursales for a in accs_kam),
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
        num_cuentas=len(accounts), total_sucursales=total_sucursales,
        suc_aromatex=suc_aromatex, suc_pestex=suc_pestex,
        facturado_periodo=facturado_periodo, pagado_periodo=pagado_periodo,
        pendiente_periodo=pendiente_periodo,
        vencido_periodo=vencido_periodo, por_cobrar_periodo=por_cobrar_periodo,
        delta_facturado=delta_facturado, delta_pagado=delta_pagado,
        delta_pendiente=delta_pendiente,
        top_riesgo=top_riesgo, cat_counts=cat_counts,
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
    en_riesgo = sum(1 for a in accounts if scores_map.get(str(a.id), {}).get("score", 100) < 40)

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
def editar_cliente(account_id):
    acc = db.session.get(CSAccount, account_id)
    if not acc:
        return "No encontrado", 404
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
def crear_cliente():
    """Crear nueva CSAccount. Solo admin/director (no KAM)."""
    if _is_kam():
        return "Sin permisos — los KAMs no crean cuentas", 403
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
def eliminar_cliente(account_id):
    """Elimina una CSAccount y todos sus registros relacionados.
    SOLO super_admin (no KAM, no director — es operación destructiva)."""
    rol = session.get("user_rol", "").lower().replace(" ", "_")
    if rol != "super_admin":
        return "Sin permisos — solo super_admin elimina cuentas", 403
    acc = db.session.get(CSAccount, account_id)
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
def account_detail(account_id):
    inicio, fin, periodo_label, periodo_param = _get_periodo()

    account = db.session.get(CSAccount, account_id)
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

    accounts = CSAccount.query.filter_by(kam_id=kam.id).order_by(CSAccount.mrr.desc()).all()
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
    accounts = CSAccount.query.all()
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

    accounts = CSAccount.query.order_by(CSAccount.nombre).all()
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
    opp = CSOpportunity(
        account_id=account_id if account_id else None,
        prospecto_nombre=request.form.get("prospecto_nombre", "").strip(),
        contacto=request.form.get("contacto", "").strip(),
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
    if opp:
        nueva = request.form.get("etapa", "")
        if nueva in [e[0] for e in ETAPAS_PIPELINE]:
            opp.etapa = nueva
            db.session.commit()
    return redirect(url_for("cs.oportunidades"))


@cs_bp.route("/oportunidades/<uuid:opp_id>/delete", methods=["POST"])
def eliminar_oportunidad(opp_id):
    opp = db.session.get(CSOpportunity, opp_id)
    if opp:
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
    accounts = CSAccount.query.order_by(CSAccount.nombre).all()
    # Conteos actuales
    num_invoices = CSInvoice.query.count()
    num_appointments = CSAppointment.query.count()
    return render_template(
        "cs_cargar_datos.html",
        accounts=accounts, num_invoices=num_invoices,
        num_appointments=num_appointments, **_ctx(),
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
def cargar_cobros():
    """Procesa CSV de cobros/facturas."""
    file = request.files.get("archivo")
    if not file or not (file.filename or "").lower().endswith(".csv"):
        flash("Subí un archivo .csv válido (cualquier mayúscula/minúscula).", "error")
        return redirect(url_for("cs.cargar_datos"))

    # Build account name + client_id maps
    accounts = CSAccount.query.all()
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
            batch.append({
                "account_id": acc_id,
                "folio": row.get("Folio", ""),
                "serie": row.get("Serie de Folio", ""),
                "concepto": row.get("Concepto", ""),
                "uen": row.get("UEN", "") or row.get("UN", ""),
                "subtotal": _parse_money(row.get("Monto Subtotal")),
                "impuestos": _parse_money(row.get("Impuestos")),
                "total": _parse_money(row.get("Total")),
                "pendiente": _parse_money(row.get("Pendiente")),
                "pagado": _parse_money(row.get("Pagado")),
                "fecha_cobro": _parse_date_cobros(row.get("Fecha de Cobro")),
                "fecha_vencimiento": _parse_date_cobros(row.get("Fecha de Vencimiento")),
                "fecha_pago": _parse_date_cobros(row.get("Fecha de Pago")),
                "estatus": row.get("Estatus", ""),
            })
            insertados += 1
            if len(batch) >= 500:
                db.session.execute(CSInvoice.__table__.insert(), batch)
                db.session.commit()
                batch = []
        except Exception:
            errores += 1

    if batch:
        db.session.execute(CSInvoice.__table__.insert(), batch)
    db.session.commit()

    _recalcular_facturacion(accounts)

    logging.warning(f"[COBROS DEBUG] no_match samples: {debug_no_match}")
    logging.warning(f"[COBROS DEBUG] CSV columns: {reader.fieldnames}")

    return render_template(
        "cs_cargar_resultado.html",
        tipo="Cobros", insertados=insertados, no_match=no_match,
        errores=errores, total=insertados + no_match + errores,
        debug_info=f"client_id_map keys: {list(client_id_map.keys())[:20]} | no_match samples: {debug_no_match}",
        **_ctx(),
    )


@cs_bp.route("/cargar-datos/citas", methods=["POST"])
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
    from sqlalchemy import inspect as _sa_inspect
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    file = request.files.get("archivo")
    if not file or not (file.filename or "").lower().endswith(".csv"):
        flash("Subí un archivo .csv válido (cualquier mayúscula/minúscula). No se procesó nada.", "error")
        return redirect(url_for("cs.cargar_datos"))

    accounts = CSAccount.query.all()
    accounts_map = {a.nombre: str(a.id) for a in accounts}

    try:
        content = file.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        flash("El archivo no es UTF-8. Guardalo como CSV UTF-8 desde Excel/Sheets y reintentá.", "error")
        return redirect(url_for("cs.cargar_datos"))

    reader = csv.DictReader(io.StringIO(content))
    columnas_detectadas = reader.fieldnames or []

    has_zoho_col = "zoho_appointment_id" in {
        c["name"] for c in _sa_inspect(db.engine).get_columns("cs_appointments")
    }

    insertados = 0
    actualizados = 0
    no_match = 0
    errores = 0
    samples_no_match: list[str] = []
    samples_error: list[str] = []
    batch: list[dict] = []

    def _flush(buf: list[dict]) -> tuple[int, int, int]:
        """Upsert por zoho_appointment_id si existe la columna y la fila trae ID.
        Si no, INSERT plano. Fallback fila por fila si una rompe."""
        if not buf:
            return 0, 0, 0

        rows_con_id = [r for r in buf if has_zoho_col and r.get("zoho_appointment_id")]
        rows_sin_id = [r for r in buf if not has_zoho_col or not r.get("zoho_appointment_id")]
        ok_ins, ok_upd, fail = 0, 0, 0

        # Upsert (idempotente)
        if rows_con_id:
            try:
                stmt = pg_insert(CSAppointment.__table__).values(rows_con_id)
                update_cols = {
                    c: stmt.excluded[c] for c in (
                        "account_id", "propiedad", "direccion", "zona", "tecnico",
                        "fecha_inicio", "fecha_terminacion", "estatus",
                        "titulo_servicio", "cantidad",
                    )
                }
                stmt = stmt.on_conflict_do_update(
                    index_elements=["zoho_appointment_id"], set_=update_cols,
                )
                db.session.execute(stmt)
                db.session.commit()
                ok_ins += len(rows_con_id)
            except Exception as e:
                db.session.rollback()
                _logging.warning("[CITAS] upsert falló (%s) — fallback fila por fila", e)
                for r in rows_con_id:
                    try:
                        stmt_one = pg_insert(CSAppointment.__table__).values([r])
                        update_cols = {
                            c: stmt_one.excluded[c] for c in (
                                "account_id", "propiedad", "direccion", "zona", "tecnico",
                                "fecha_inicio", "fecha_terminacion", "estatus",
                                "titulo_servicio", "cantidad",
                            )
                        }
                        stmt_one = stmt_one.on_conflict_do_update(
                            index_elements=["zoho_appointment_id"], set_=update_cols,
                        )
                        db.session.execute(stmt_one)
                        db.session.commit()
                        ok_ins += 1
                    except Exception as e2:
                        db.session.rollback()
                        fail += 1
                        if len(samples_error) < 3:
                            samples_error.append(f"{type(e2).__name__}: {str(e2)[:120]}")

        # INSERT plano para filas sin ID o si no hay columna
        if rows_sin_id:
            try:
                db.session.execute(CSAppointment.__table__.insert(), rows_sin_id)
                db.session.commit()
                ok_ins += len(rows_sin_id)
            except Exception as e:
                db.session.rollback()
                _logging.warning("[CITAS] bulk insert falló (%s) — fallback fila por fila", e)
                for r in rows_sin_id:
                    try:
                        db.session.execute(CSAppointment.__table__.insert(), [r])
                        db.session.commit()
                        ok_ins += 1
                    except Exception as e2:
                        db.session.rollback()
                        fail += 1
                        if len(samples_error) < 3:
                            samples_error.append(f"{type(e2).__name__}: {str(e2)[:120]}")

        return ok_ins, ok_upd, fail

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
            }
            if has_zoho_col and visita_id:
                r_dict["zoho_appointment_id"] = visita_id[:64]
            batch.append(r_dict)

            if len(batch) >= 500:
                ins, upd, fail = _flush(batch)
                insertados += ins
                actualizados += upd
                errores += fail
                batch = []
        except (ValueError, TypeError) as e:
            errores += 1
            if len(samples_error) < 3:
                samples_error.append(f"row parse: {str(e)[:120]}")

    if batch:
        ins, upd, fail = _flush(batch)
        insertados += ins
        actualizados += upd
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
# CRUD de citas por cuenta — para que el KAM gestione su plantilla activa
# sin depender de la carga masiva (que arrastra cancelaciones).
# ──────────────────────────────────────────────


def _can_edit_account(account):
    """KAM solo puede editar sus propias cuentas. Admin/director: todas."""
    if not _is_kam():
        return True
    return str(account.kam_id) == str(_current_kam_id())


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
def listar_citas_cuenta(account_id):
    """GET /cs/api/cuentas/<id>/citas?solo_activas=1 — citas filtradas.
    Default: solo_activas=1 (oculta Cancelada / No Realizada / Archivada)."""
    account = db.session.get(CSAccount, account_id)
    if not account:
        return jsonify({"error": "Cuenta no encontrada"}), 404
    if not _can_edit_account(account):
        return jsonify({"error": "Sin permisos"}), 403

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
def crear_cita(account_id):
    """KAM agrega una cita a la plantilla activa. Default estatus 'Agendada'."""
    account = db.session.get(CSAccount, account_id)
    if not account:
        return jsonify({"error": "Cuenta no encontrada"}), 404
    if not _can_edit_account(account):
        return jsonify({"error": "Sin permisos"}), 403

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
    account = db.session.get(CSAccount, cita.account_id)
    if not _can_edit_account(account):
        return jsonify({"error": "Sin permisos"}), 403

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
    account = db.session.get(CSAccount, cita.account_id)
    if not _can_edit_account(account):
        return jsonify({"error": "Sin permisos"}), 403
    cita.estatus = "Cancelada"
    db.session.commit()
    return jsonify({"ok": True})


@cs_bp.route("/api/citas/<uuid:cita_id>", methods=["DELETE"])
def eliminar_cita(cita_id):
    """Hard delete: solo super_admin / director. KAMs usan /cancelar."""
    if _is_kam():
        return jsonify({"error": "KAMs usan /cancelar (soft delete)"}), 403
    cita = db.session.get(CSAppointment, cita_id)
    if not cita:
        return jsonify({"error": "Cita no encontrada"}), 404
    db.session.delete(cita)
    db.session.commit()
    return jsonify({"ok": True})


@cs_bp.route("/cargar-datos/limpiar/<tipo>", methods=["POST"])
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
    accounts = CSAccount.query.order_by(CSAccount.nombre).all()
    return render_template(
        "cs_contactos.html",
        contactos=contactos, accounts=accounts, buscar=buscar, **_ctx(),
    )


@cs_bp.route("/contactos/crear", methods=["POST"])
def crear_contacto():
    account_id = request.form.get("account_id", "").strip()
    if not account_id:
        return redirect(url_for("cs.contactos_directory"))
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
def crear_incidencia(account_id):
    tipo = request.form.get("tipo", "").strip()
    if not tipo:
        return redirect(url_for("cs.account_detail", account_id=account_id) + "?tab=incidencias")

    propiedad_id = request.form.get("propiedad_id", "").strip() or None
    propiedad_nombre = ""
    if propiedad_id:
        prop = db.session.get(CSPropiedad, propiedad_id)
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
def cambiar_status_incidencia(account_id, inc_id):
    inc = db.session.get(CSIncidencia, inc_id)
    if inc:
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
def descargar_template_propiedades(account_id):
    """Plantilla CSV con headers + 2 filas de ejemplo."""
    acc = db.session.get(CSAccount, account_id)
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
def upload_propiedades_csv(account_id):
    """Procesa CSV de sucursales y upserta en cs_propiedades.
    Después calcula cross-ref con citas existentes (match por nombre, ci)."""
    acc = db.session.get(CSAccount, account_id)
    if not acc:
        flash("Cuenta no encontrada", "error")
        return redirect(url_for("cs.clientes"))

    if _is_kam() and str(acc.kam_id) != str(_current_kam_id()):
        flash("Solo podés cargar sucursales de tus propias cuentas", "error")
        return redirect(url_for("cs.clientes"))

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
def eliminar_entregable(account_id, ent_id):
    e = db.session.get(CSEntregable, ent_id)
    if e:
        db.session.delete(e)
        db.session.commit()
    return redirect(url_for("cs.account_detail", account_id=account_id) + "?tab=entregables#entregables")


# ══════════════════════════════════════════════
# QBR
# ══════════════════════════════════════════════
@cs_bp.route("/account/<uuid:account_id>/qbr")
def download_qbr(account_id):
    account = db.session.get(CSAccount, account_id)
    if not account:
        return "Cuenta no encontrada", 404
    from cs_qbr_generator import generar_qbr
    excel_buffer = generar_qbr(account, trimestre="Q1 2026")
    nombre_limpio = account.nombre.replace(" ", "_").replace("/", "-")
    return send_file(
        excel_buffer, as_attachment=True,
        download_name=f"QBR_Q1_2026_{nombre_limpio}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


# ══════════════════════════════════════════════
# CRUD — KPIs, Notas, Tareas
# ══════════════════════════════════════════════
@cs_bp.route("/account/<uuid:account_id>/kpis", methods=["POST"])
def update_kpis(account_id):
    account = db.session.get(CSAccount, account_id)
    if not account:
        return "No encontrado", 404
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
def create_note(account_id):
    contenido = request.form.get("contenido", "").strip()
    autor = request.form.get("autor", "").strip()
    if contenido:
        adj = _parse_adjuntos(request.form)
        db.session.add(CSNote(account_id=account_id, autor=autor, contenido=contenido, adjuntos=adj))
        db.session.commit()
    return redirect(url_for("cs.account_detail", account_id=account_id) + "?tab=notas")


@cs_bp.route("/account/<uuid:account_id>/notes/<uuid:note_id>/delete", methods=["POST"])
def delete_note(account_id, note_id):
    note = db.session.get(CSNote, note_id)
    if note:
        db.session.delete(note)
        db.session.commit()
    return redirect(url_for("cs.account_detail", account_id=account_id) + "?tab=notas")


@cs_bp.route("/account/<uuid:account_id>/tasks", methods=["POST"])
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
def toggle_task(account_id, task_id):
    task = db.session.get(CSTask, task_id)
    if task:
        task.completada = not task.completada
        db.session.commit()
    return redirect(url_for("cs.account_detail", account_id=account_id) + "?tab=tareas")


@cs_bp.route("/account/<uuid:account_id>/tasks/<uuid:task_id>/delete", methods=["POST"])
def delete_task(account_id, task_id):
    task = db.session.get(CSTask, task_id)
    if task:
        db.session.delete(task)
        db.session.commit()
    return redirect(url_for("cs.account_detail", account_id=account_id) + "?tab=tareas")

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
    CSOnboardingAccount, CSOpportunity, CSContacto, UserCRM, RolCRM,
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


def _is_kam():
    return session.get("user_rol", "").upper() == "KAM"


def _current_kam_id():
    if _is_kam():
        return session.get("user_id")
    return None


def _ctx():
    """Context vars comunes para todos los templates."""
    return {
        "user_nombre": session.get("user_nombre", ""),
        "user_rol": session.get("user_rol", ""),
        "is_kam": _is_kam(),
    }


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
    """Calcula facturación del periodo desde cs_invoices (no campos estáticos)."""
    rows = (
        db.session.query(
            CSInvoice.account_id,
            func.coalesce(func.sum(CSInvoice.total), 0),
            func.coalesce(func.sum(CSInvoice.pagado), 0),
            func.coalesce(func.sum(CSInvoice.pendiente), 0),
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
    for acc_id, total, pagado, pendiente, num in rows:
        result[str(acc_id)] = {
            "facturado": float(total), "pagado": float(pagado),
            "pendiente": float(pendiente), "num_facturas": num,
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

    return render_template(
        "cs_dashboard.html",
        mrr_total=mrr_total, arr_total=arr_total,
        num_cuentas=len(accounts), total_sucursales=total_sucursales,
        facturado_periodo=facturado_periodo, pagado_periodo=pagado_periodo,
        pendiente_periodo=pendiente_periodo,
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

    clientes_data = []
    for acc in accounts:
        hs = scores_map[str(acc.id)]
        owners = CSContacto.query.filter_by(account_id=acc.id, is_owner=True).all()
        clientes_data.append({
            "account": acc, "health": hs,
            "owners": owners,
        })

    return render_template(
        "cs_clientes.html",
        clientes=clientes_data,
        **_ctx(),
    )


@cs_bp.route("/clientes/<uuid:account_id>/editar", methods=["POST"])
def editar_cliente(account_id):
    acc = db.session.get(CSAccount, account_id)
    if not acc:
        return "No encontrado", 404
    if "logo_url" in request.form:
        acc.logo_url = request.form.get("logo_url", "").strip()
    if "giro" in request.form:
        # Multi-select: getlist returns multiple values
        giros = request.form.getlist("giro")
        acc.giro = ",".join(g.strip() for g in giros if g.strip())
    if "tier" in request.form:
        acc.tier = request.form.get("tier", "").strip()
    db.session.commit()
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

    notes = CSNote.query.filter_by(account_id=account.id).order_by(CSNote.created_at.desc()).all()
    tasks = CSTask.query.filter_by(account_id=account.id).order_by(CSTask.completada, CSTask.fecha_limite).all()
    tareas_pendientes = sum(1 for t in tasks if not t.completada)
    contactos = CSContacto.query.filter_by(account_id=account.id).order_by(CSContacto.is_owner.desc(), CSContacto.nombre).all()

    return render_template(
        "cs_account_detail.html",
        account=account, health=health, invoices=invoices,
        total_facturado=total_facturado, total_pagado=total_pagado,
        total_pendiente=total_pendiente, facturas_pagadas=facturas_pagadas,
        facturas_pendientes=facturas_pendientes,
        appointments=appointments, citas_por_estatus=citas_por_estatus,
        notes=notes, tasks=tasks, tareas_pendientes=tareas_pendientes,
        contactos=contactos,
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


def _match_account(cliente_nombre, accounts_map):
    """Busca la cuenta por nombre parcial (case-insensitive)."""
    if not cliente_nombre:
        return None
    nombre_lower = str(cliente_nombre).lower()
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


@cs_bp.route("/cargar-datos/cobros", methods=["POST"])
def cargar_cobros():
    """Procesa CSV de cobros/facturas."""
    file = request.files.get("archivo")
    if not file or not file.filename.endswith(".csv"):
        return redirect(url_for("cs.cargar_datos"))

    # Build account name map
    accounts = CSAccount.query.all()
    accounts_map = {a.nombre: str(a.id) for a in accounts}

    content = file.read().decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(content))

    insertados = 0
    no_match = 0
    errores = 0

    for row in reader:
        cliente = row.get("Cliente", "").strip()
        acc_id = _match_account(cliente, accounts_map)
        if not acc_id:
            no_match += 1
            continue

        try:
            inv = CSInvoice(
                account_id=acc_id,
                folio=row.get("Folio", ""),
                serie=row.get("Serie de Folio", ""),
                concepto=row.get("Concepto", ""),
                uen=row.get("UEN", ""),
                subtotal=_parse_money(row.get("Monto Subtotal")),
                impuestos=_parse_money(row.get("Impuestos")),
                total=_parse_money(row.get("Total")),
                pendiente=_parse_money(row.get("Pendiente")),
                pagado=_parse_money(row.get("Pagado")),
                fecha_cobro=_parse_date_cobros(row.get("Fecha de Cobro")),
                fecha_vencimiento=_parse_date_cobros(row.get("Fecha de Vencimiento")),
                fecha_pago=_parse_date_cobros(row.get("Fecha de Pago")),
                estatus=row.get("Estatus", ""),
            )
            db.session.add(inv)
            insertados += 1
        except Exception:
            errores += 1

    db.session.commit()

    # Actualizar totales en cs_accounts
    _recalcular_facturacion(accounts)

    return render_template(
        "cs_cargar_resultado.html",
        tipo="Cobros", insertados=insertados, no_match=no_match,
        errores=errores, total=insertados + no_match + errores,
        **_ctx(),
    )


@cs_bp.route("/cargar-datos/citas", methods=["POST"])
def cargar_citas():
    """Procesa CSV de citas/operación."""
    file = request.files.get("archivo")
    if not file or not file.filename.endswith(".csv"):
        return redirect(url_for("cs.cargar_datos"))

    accounts = CSAccount.query.all()
    accounts_map = {a.nombre: str(a.id) for a in accounts}

    content = file.read().decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(content))

    insertados = 0
    no_match = 0
    errores = 0

    for row in reader:
        cliente = row.get("Cliente", "").strip()
        acc_id = _match_account(cliente, accounts_map)
        if not acc_id:
            no_match += 1
            continue

        try:
            apt = CSAppointment(
                account_id=acc_id,
                propiedad=row.get("Propiedad", ""),
                direccion=row.get("Dirección", row.get("Direccion", "")),
                zona=row.get("Zona", ""),
                tecnico=row.get("Tecnico", row.get("Técnico", "")),
                fecha_inicio=_parse_datetime_citas(row.get("Fecha de Inicio")),
                fecha_terminacion=_parse_datetime_citas(row.get("Fecha de Terminación", row.get("Fecha de Terminacion", ""))),
                estatus=row.get("Estatus", ""),
                titulo_servicio=row.get("Titulo Servicio", row.get("Título Servicio", "")),
                cantidad=int(float(row.get("Cantidad", 1) or 1)),
            )
            db.session.add(apt)
            insertados += 1
        except Exception:
            errores += 1

    db.session.commit()
    return render_template(
        "cs_cargar_resultado.html",
        tipo="Citas", insertados=insertados, no_match=no_match,
        errores=errores, total=insertados + no_match + errores,
        **_ctx(),
    )


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
        db.session.add(CSNote(account_id=account_id, autor=autor, contenido=contenido))
        db.session.commit()
    return redirect(url_for("cs.account_detail", account_id=account_id) + "#notas")


@cs_bp.route("/account/<uuid:account_id>/notes/<uuid:note_id>/delete", methods=["POST"])
def delete_note(account_id, note_id):
    note = db.session.get(CSNote, note_id)
    if note:
        db.session.delete(note)
        db.session.commit()
    return redirect(url_for("cs.account_detail", account_id=account_id) + "#notas")


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
        db.session.add(CSTask(
            account_id=account_id, tipo=request.form.get("tipo", "check-in"),
            descripcion=descripcion, responsable=request.form.get("responsable", ""),
            fecha_limite=fecha_limite,
        ))
        db.session.commit()
    return redirect(url_for("cs.account_detail", account_id=account_id) + "#tareas")


@cs_bp.route("/account/<uuid:account_id>/tasks/<uuid:task_id>/toggle", methods=["POST"])
def toggle_task(account_id, task_id):
    task = db.session.get(CSTask, task_id)
    if task:
        task.completada = not task.completada
        db.session.commit()
    return redirect(url_for("cs.account_detail", account_id=account_id) + "#tareas")


@cs_bp.route("/account/<uuid:account_id>/tasks/<uuid:task_id>/delete", methods=["POST"])
def delete_task(account_id, task_id):
    task = db.session.get(CSTask, task_id)
    if task:
        db.session.delete(task)
        db.session.commit()
    return redirect(url_for("cs.account_detail", account_id=account_id) + "#tareas")

# blueprints/cs.py
"""
CS Dashboard — Customer Success para KAMs.
Rutas bajo /cs/
"""
from datetime import datetime, date
from flask import Blueprint, render_template, request, redirect, url_for, session, jsonify, send_file
from sqlalchemy import func
from extensions import db
from models import (
    CSAccount, CSInvoice, CSAppointment, CSNote, CSTask,
    CSOnboardingAccount, CSOpportunity, UserCRM, RolCRM,
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
    if len(periodo_param) == 7 and periodo_param != "all":
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

    return render_template(
        "cs_account_detail.html",
        account=account, health=health, invoices=invoices,
        total_facturado=total_facturado, total_pagado=total_pagado,
        total_pendiente=total_pendiente, facturas_pagadas=facturas_pagadas,
        facturas_pendientes=facturas_pendientes,
        appointments=appointments, citas_por_estatus=citas_por_estatus,
        notes=notes, tasks=tasks, tareas_pendientes=tareas_pendientes,
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
    """MRR facturado por mes (últimos 6 meses con datos)."""
    rows = (
        db.session.query(
            func.date_trunc("month", CSInvoice.fecha_cobro).label("mes"),
            func.sum(CSInvoice.total),
            func.sum(CSInvoice.pagado),
            func.sum(CSInvoice.pendiente),
        )
        .filter(CSInvoice.fecha_cobro.isnot(None))
        .group_by("mes")
        .order_by("mes")
        .all()
    )
    return jsonify([{
        "mes": r[0].strftime("%Y-%m") if r[0] else "",
        "mes_label": r[0].strftime("%b %Y") if r[0] else "",
        "facturado": float(r[1] or 0),
        "pagado": float(r[2] or 0),
        "pendiente": float(r[3] or 0),
    } for r in rows])


@cs_bp.route("/api/operacion-trend")
def api_operacion_trend():
    """Citas por estatus por mes."""
    from sqlalchemy import case, extract
    rows = (
        db.session.query(
            func.date_trunc("month", CSAppointment.fecha_inicio).label("mes"),
            func.count(CSAppointment.id).label("total"),
            func.sum(case((CSAppointment.estatus == "Terminada", 1), else_=0)).label("terminadas"),
            func.sum(case((CSAppointment.estatus == "Cancelada", 1), else_=0)).label("canceladas"),
            func.sum(case((CSAppointment.estatus == "No Realizada", 1), else_=0)).label("no_realizadas"),
        )
        .filter(CSAppointment.fecha_inicio.isnot(None))
        .group_by("mes")
        .order_by("mes")
        .all()
    )
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

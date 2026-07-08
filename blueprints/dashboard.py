# blueprints/dashboard.py
"""
API para metricas del dashboard.
Filtra automaticamente por vendedor si el usuario logueado es vendedor.
"""
import csv
import io
from datetime import date, timedelta
from flask import Blueprint, request, jsonify, session, Response
from sqlalchemy import func
from extensions import db
from models import Lead, EtapaPipeline, OrigenLead, GastoPublicidad, PlataformaAds, MetaCampaign
from blueprints.auth import get_vendedor_filter, require_role
import scip_meta

dashboard_bp = Blueprint("dashboard", __name__)


def _apply_vendedor_filter(query):
    """Filtra query de leads por vendedor si no es super_admin."""
    vid = get_vendedor_filter()
    if vid:
        query = query.filter(Lead.usuario_asignado_id == vid)
    return query


def _apply_un_filter(query):
    """FEAT-2026-06-29: filtra query de Lead por la UN del request.
    No-op si no viene ?un= o si la UN es 'todas' / inválida."""
    from un_filter import filtrar_leads_por_un
    return filtrar_leads_por_un(query, Lead, request.args.get("un"))


def _get_date_range(mes_param):
    """Retorna (inicio_mes, fin_mes) a partir de param ?mes=2026-04."""
    if mes_param:
        year, month = mes_param.split("-")
        inicio = date(int(year), int(month), 1)
    else:
        inicio = date.today().replace(day=1)
    if inicio.month == 12:
        fin = inicio.replace(year=inicio.year + 1, month=1)
    else:
        fin = inicio.replace(month=inicio.month + 1)
    return inicio, fin


def _month_label(inicio):
    meses = [
        "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
        "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre",
    ]
    return f"{meses[inicio.month - 1]} {inicio.year}"


def _iter_months(inicio, fin):
    cur = inicio.replace(day=1)
    end = fin.replace(day=1)
    while cur <= end:
        yield cur
        if cur.month == 12:
            cur = cur.replace(year=cur.year + 1, month=1)
        else:
            cur = cur.replace(month=cur.month + 1)


def _meta_date_range(inicio, fin):
    """Meta Insights usa rangos inclusivos; fin_mes en el CRM es exclusivo."""
    until = fin - timedelta(days=1)
    return inicio.isoformat(), until.isoformat()


def _manual_spend(inicio, fin, marca_filter=None, exclude_meta=False):
    q = GastoPublicidad.query.filter(
        GastoPublicidad.fecha >= inicio,
        GastoPublicidad.fecha < fin,
    )
    if marca_filter:
        q = q.filter(GastoPublicidad.marca == marca_filter)
    if exclude_meta:
        q = q.filter(GastoPublicidad.plataforma.notin_([
            PlataformaAds.FACEBOOK,
            PlataformaAds.INSTAGRAM,
        ]))
    total = q.with_entities(func.coalesce(func.sum(GastoPublicidad.monto), 0)).scalar()
    return float(total or 0)


def _get_meta_ads_spend(inicio, fin, marca_filter=None):
    """Suma gasto real de Meta API usando campañas registradas en meta_campaigns."""
    result = {
        "available": False,
        "spend": 0.0,
        "has_registered_campaigns": False,
        "campaigns": [],
        "errors": [],
    }
    if not scip_meta.is_configured():
        result["errors"].append("META_ACCESS_TOKEN no configurado")
        return result

    result["available"] = True
    since, until = _meta_date_range(inicio, fin)
    campaigns_q = MetaCampaign.query.filter(MetaCampaign.activa.is_(True))
    if marca_filter:
        campaigns_q = campaigns_q.filter(MetaCampaign.marca == marca_filter)

    registered_campaigns = campaigns_q.order_by(MetaCampaign.marca, MetaCampaign.nombre).all()
    result["has_registered_campaigns"] = bool(registered_campaigns)

    for campaign in registered_campaigns:
        try:
            metrics = scip_meta.get_campaign_metrics(
                campaign.campaign_id,
                date_range=(since, until),
            )
            spend = float(metrics.get("spend") or 0)
            result["spend"] += spend
            result["campaigns"].append({
                "campaign_id": campaign.campaign_id,
                "nombre": campaign.nombre,
                "marca": campaign.marca,
                "unidad": campaign.unidad,
                "spend": spend,
            })
        except Exception as exc:
            result["errors"].append({
                "campaign_id": campaign.campaign_id,
                "nombre": campaign.nombre,
                "error": str(exc),
            })

    result["spend"] = round(result["spend"], 2)
    return result


@dashboard_bp.route("/pipeline-valores", methods=["GET"])
def pipeline_valores():
    q = db.session.query(
        Lead.etapa_pipeline,
        func.count(Lead.id).label("cantidad"),
        func.coalesce(func.sum(func.coalesce(
            Lead.factura_monto,
            Lead.cantidad_productos * Lead.precio_unitario,
            Lead.valor_estimado, 0,
        )), 0).label("valor_total"),
    )

    vid = get_vendedor_filter()
    if vid:
        q = q.filter(Lead.usuario_asignado_id == vid)
    # FEAT-2026-06-29: filtro global por UN
    q = _apply_un_filter(q)

    resultados = q.group_by(Lead.etapa_pipeline).all()

    data = {}
    for etapa_enum, cantidad, valor in resultados:
        data[etapa_enum.value] = {"cantidad": cantidad, "valor": float(valor)}

    for etapa in EtapaPipeline:
        if etapa.value not in data:
            data[etapa.value] = {"cantidad": 0, "valor": 0}

    return jsonify(data)


@dashboard_bp.route("/meses", methods=["GET"])
def meses_disponibles():
    """Meses para filtros del dashboard, desde el primer registro hasta hoy."""
    first_lead = db.session.query(func.min(Lead.fecha_creacion)).scalar()
    first_gasto = db.session.query(func.min(GastoPublicidad.fecha)).scalar()
    candidates = []
    for d in (first_lead, first_gasto):
        if d:
            candidates.append(d.date() if hasattr(d, "date") else d)
    start = min(candidates) if candidates else date.today()
    today = date.today()
    months = [
        {"value": m.strftime("%Y-%m"), "label": _month_label(m)}
        for m in _iter_months(start.replace(day=1), today.replace(day=1))
    ]
    months.reverse()
    return jsonify(months)


@dashboard_bp.route("/embudo", methods=["GET"])
def embudo():
    mes_param = request.args.get("mes")
    inicio_mes, fin_mes = _get_date_range(mes_param)

    leads_q = Lead.query.filter(
        Lead.fecha_creacion >= inicio_mes,
        Lead.fecha_creacion < fin_mes,
    )
    leads_q = _apply_vendedor_filter(leads_q)
    leads_q = _apply_un_filter(leads_q)

    total = leads_q.count()

    etapas_calificadas = [
        EtapaPipeline.CONTACTO_1, EtapaPipeline.CONTACTO_2,
        EtapaPipeline.CONTACTO_3, EtapaPipeline.CONTACTO_4,
        EtapaPipeline.COTIZACION, EtapaPipeline.DEMO,
        EtapaPipeline.NEGOCIACION, EtapaPipeline.CIERRE_GANADO,
    ]
    etapas_cotizadas = [
        EtapaPipeline.COTIZACION, EtapaPipeline.DEMO,
        EtapaPipeline.NEGOCIACION, EtapaPipeline.CIERRE_GANADO,
    ]

    calificados = leads_q.filter(Lead.etapa_pipeline.in_(etapas_calificadas)).count()
    cotizados = leads_q.filter(Lead.etapa_pipeline.in_(etapas_cotizadas)).count()
    ganados = leads_q.filter(Lead.etapa_pipeline == EtapaPipeline.CIERRE_GANADO).count()
    perdidos = leads_q.filter(Lead.etapa_pipeline == EtapaPipeline.CIERRE_PERDIDO).count()

    # Breakdown por origen del lead (Meta Ads, Web, WhatsApp Orgánico, etc.)
    origen_rows = (
        leads_q.with_entities(Lead.origen, func.count(Lead.id))
        .group_by(Lead.origen).all()
    )
    leads_por_origen = []
    for origen_enum, n in origen_rows:
        label = origen_enum.value if origen_enum else "Sin origen"
        leads_por_origen.append({"origen": label, "count": int(n)})
    # Ordenar de mayor a menor count para el UI
    leads_por_origen.sort(key=lambda x: -x["count"])

    # Revenue
    rev_q = db.session.query(func.coalesce(func.sum(func.coalesce(
        Lead.factura_monto,
        Lead.cantidad_productos * Lead.precio_unitario, Lead.valor_estimado, 0,
    )), 0)).filter(
        Lead.fecha_creacion >= inicio_mes, Lead.fecha_creacion < fin_mes,
        Lead.etapa_pipeline == EtapaPipeline.CIERRE_GANADO,
    )
    vid = get_vendedor_filter()
    if vid:
        rev_q = rev_q.filter(Lead.usuario_asignado_id == vid)
    rev_q = _apply_un_filter(rev_q)
    revenue = float(rev_q.scalar() or 0)

    # Pipe Activo: SIN filtro de fecha (es snapshot actual, no del mes) y
    # SIN cerrados (ni ganado ni perdido) para que cuadre con las barras de
    # etapas abiertas del dashboard.
    pipe_q = db.session.query(func.coalesce(func.sum(func.coalesce(
        Lead.factura_monto,
        Lead.cantidad_productos * Lead.precio_unitario, Lead.valor_estimado, 0,
    )), 0)).filter(
        Lead.etapa_pipeline.notin_([
            EtapaPipeline.CIERRE_PERDIDO, EtapaPipeline.CIERRE_GANADO,
        ]),
    )
    if vid:
        pipe_q = pipe_q.filter(Lead.usuario_asignado_id == vid)
    pipe_q = _apply_un_filter(pipe_q)
    pipe_total = float(pipe_q.scalar() or 0)

    # Gastos (solo super_admin ve gastos reales, vendedor ve 0)
    gasto_ads = 0.0
    gasto_ads_manual = 0.0
    meta_ads_api = {
        "available": False,
        "spend": 0.0,
        "has_registered_campaigns": False,
        "campaigns": [],
        "errors": [],
    }
    if not vid:
        meta_ads_api = _get_meta_ads_spend(inicio_mes, fin_mes)
        gasto_ads_manual = _manual_spend(
            inicio_mes,
            fin_mes,
            exclude_meta=meta_ads_api["has_registered_campaigns"],
        )
        gasto_ads = round(gasto_ads_manual + meta_ads_api["spend"], 2)

    costo_por_lead = round(gasto_ads / total, 2) if total > 0 else 0
    costo_por_cierre = round(gasto_ads / ganados, 2) if ganados > 0 else 0
    roi = round(revenue / gasto_ads, 2) if gasto_ads > 0 else 0

    return jsonify({
        "mes": inicio_mes.strftime("%Y-%m"),
        "leads_totales": total,
        "leads_por_origen": leads_por_origen,
        "calificados": calificados,
        "cotizados": cotizados,
        "ganados": ganados,
        "perdidos": perdidos,
        "revenue_ganado": revenue,
        "pipe_total": pipe_total,
        "gasto_ads": gasto_ads,
        "gasto_ads_manual": gasto_ads_manual,
        "gasto_ads_meta": meta_ads_api["spend"],
        "meta_ads_api": meta_ads_api,
        "costo_por_lead": costo_por_lead,
        "costo_por_cierre": costo_por_cierre,
        "roi": roi,
    })


# ──────────────────────────────────────────────
# Gastos — solo super_admin
# ──────────────────────────────────────────────
@dashboard_bp.route("/gastos", methods=["GET"])
def listar_gastos():
    mes_param = request.args.get("mes")
    marca = request.args.get("marca")
    q = GastoPublicidad.query
    if mes_param:
        inicio, fin = _get_date_range(mes_param)
        q = q.filter(GastoPublicidad.fecha >= inicio, GastoPublicidad.fecha < fin)
    if marca:
        q = q.filter(GastoPublicidad.marca == marca)
    gastos = q.order_by(GastoPublicidad.fecha.desc()).all()
    return jsonify([g.to_dict() for g in gastos])


@dashboard_bp.route("/gastos", methods=["POST"])
@require_role(["super_admin"])
def registrar_gasto():
    data = request.get_json() or {}
    try:
        plataforma = PlataformaAds(data["plataforma"])
    except (ValueError, KeyError):
        return jsonify({"error": "Plataforma invalida"}), 400

    gasto = GastoPublicidad(
        plataforma=plataforma, marca=data.get("marca"),
        campana=data.get("campana"), monto=data["monto"],
        fecha=date.fromisoformat(data["fecha"]), notas=data.get("notas"),
    )
    db.session.add(gasto)
    db.session.commit()
    return jsonify(gasto.to_dict()), 201


@dashboard_bp.route("/gastos/<uuid:gasto_id>", methods=["DELETE"])
@require_role(["super_admin"])
def eliminar_gasto(gasto_id):
    gasto = db.session.get(GastoPublicidad, gasto_id)
    if not gasto:
        return jsonify({"error": "No encontrado"}), 404
    db.session.delete(gasto)
    db.session.commit()
    return jsonify({"ok": True})


# ── Marketing ROI: embudo + spend segmentado por canal ────────────


# Mapeo PlataformaAds → OrigenLead (para attribución)
_PLATAFORMA_TO_ORIGEN = {
    PlataformaAds.FACEBOOK:  OrigenLead.META_ADS,
    PlataformaAds.INSTAGRAM: OrigenLead.META_ADS,
    PlataformaAds.GOOGLE:    OrigenLead.WEB,    # asumimos paid search → leads que llegan vía Web
    PlataformaAds.TIKTOK:    OrigenLead.WEB,
    PlataformaAds.OTRO:      None,              # no atribuye, queda en bucket "sin atribuir"
}


def _empty_bucket():
    return {
        "leads": 0, "calificados": 0, "cotizados": 0,
        "ganados": 0, "perdidos": 0,
        "spend": 0.0, "revenue": 0.0,
        "cpl": 0.0, "cac": 0.0, "roi": 0.0, "tasa_cierre": 0.0,
    }


@dashboard_bp.route("/marketing-roi", methods=["GET"])
def marketing_roi():
    """Embudo + spend segmentado por origen. Devuelve per-canal y totales.
    Filtros: ?mes=YYYY-MM (default mes actual), ?marca=Aromatex (opcional)."""
    mes_param = request.args.get("mes")
    inicio_mes, fin_mes = _get_date_range(mes_param)
    marca_filter = request.args.get("marca")

    etapas_calif = [
        EtapaPipeline.CONTACTO_1, EtapaPipeline.CONTACTO_2,
        EtapaPipeline.CONTACTO_3, EtapaPipeline.CONTACTO_4,
        EtapaPipeline.COTIZACION, EtapaPipeline.DEMO,
        EtapaPipeline.NEGOCIACION, EtapaPipeline.CIERRE_GANADO,
    ]
    etapas_cotiz = [
        EtapaPipeline.COTIZACION, EtapaPipeline.DEMO,
        EtapaPipeline.NEGOCIACION, EtapaPipeline.CIERRE_GANADO,
    ]

    # Base query Leads del período
    leads_q = Lead.query.filter(
        Lead.fecha_creacion >= inicio_mes,
        Lead.fecha_creacion < fin_mes,
    )
    leads_q = _apply_vendedor_filter(leads_q)
    leads_q = _apply_un_filter(leads_q)
    if marca_filter:
        leads_q = leads_q.filter(Lead.marca_interes == marca_filter)

    # Buckets por origen
    por_origen = {o.value: _empty_bucket() for o in OrigenLead}
    sin_origen = _empty_bucket()  # leads sin origen marcado

    # Counts por origen
    rows = (
        leads_q.with_entities(Lead.origen, func.count(Lead.id))
        .group_by(Lead.origen).all()
    )
    for orig, cnt in rows:
        b = por_origen[orig.value] if orig else sin_origen
        b["leads"] = int(cnt)

    # Calificados/cotizados/ganados/perdidos por origen
    for etapa_list, key in [
        (etapas_calif, "calificados"), (etapas_cotiz, "cotizados"),
        ([EtapaPipeline.CIERRE_GANADO], "ganados"),
        ([EtapaPipeline.CIERRE_PERDIDO], "perdidos"),
    ]:
        sub = (
            leads_q.filter(Lead.etapa_pipeline.in_(etapa_list))
            .with_entities(Lead.origen, func.count(Lead.id))
            .group_by(Lead.origen).all()
        )
        for orig, cnt in sub:
            b = por_origen[orig.value] if orig else sin_origen
            b[key] = int(cnt)

    # Revenue por origen (suma de cantidad*precio o valor_estimado de ganados)
    rev_rows = (
        leads_q.filter(Lead.etapa_pipeline == EtapaPipeline.CIERRE_GANADO)
        .with_entities(
            Lead.origen,
            func.coalesce(func.sum(func.coalesce(
                Lead.factura_monto,
                Lead.cantidad_productos * Lead.precio_unitario,
                Lead.valor_estimado, 0,
            )), 0),
        ).group_by(Lead.origen).all()
    )
    for orig, rev in rev_rows:
        b = por_origen[orig.value] if orig else sin_origen
        b["revenue"] = float(rev or 0)

    # Spend por origen (solo super_admin lo ve real, vendedor=0)
    vid = get_vendedor_filter()
    manual_spend_total = 0.0
    meta_ads_api = {
        "available": False,
        "spend": 0.0,
        "has_registered_campaigns": False,
        "campaigns": [],
        "errors": [],
    }
    if not vid:
        meta_ads_api = _get_meta_ads_spend(inicio_mes, fin_mes, marca_filter)
        por_origen[OrigenLead.META_ADS.value]["spend"] += meta_ads_api["spend"]

        gastos_q = GastoPublicidad.query.filter(
            GastoPublicidad.fecha >= inicio_mes,
            GastoPublicidad.fecha < fin_mes,
        )
        if marca_filter:
            gastos_q = gastos_q.filter(GastoPublicidad.marca == marca_filter)
        for g in gastos_q.all():
            if meta_ads_api["has_registered_campaigns"] and g.plataforma in (
                PlataformaAds.FACEBOOK,
                PlataformaAds.INSTAGRAM,
            ):
                continue
            origen = _PLATAFORMA_TO_ORIGEN.get(g.plataforma)
            target = por_origen[origen.value] if origen else sin_origen
            amount = float(g.monto or 0)
            target["spend"] += amount
            manual_spend_total += amount

    # Calcular CPL, CAC, ROI, tasa cierre por bucket
    def _finish(b):
        b["cpl"] = round(b["spend"] / b["leads"], 2) if b["leads"] else 0.0
        b["cac"] = round(b["spend"] / b["ganados"], 2) if b["ganados"] else 0.0
        b["roi"] = round(b["revenue"] / b["spend"], 2) if b["spend"] else 0.0
        b["tasa_cierre"] = round((b["ganados"] / b["leads"]) * 100, 1) if b["leads"] else 0.0
    for b in por_origen.values():
        _finish(b)
    _finish(sin_origen)

    # Totales
    total = _empty_bucket()
    for k in ("leads", "calificados", "cotizados", "ganados", "perdidos", "spend", "revenue"):
        total[k] = sum(b[k] for b in por_origen.values()) + sin_origen[k]
    _finish(total)

    return jsonify({
        "mes": inicio_mes.strftime("%Y-%m"),
        "marca_filter": marca_filter,
        "total": total,
        "por_origen": por_origen,
        "sin_origen": sin_origen,
        "spend_sources": {
            "meta_api": meta_ads_api["spend"],
            "manual": round(manual_spend_total, 2),
        },
        "meta_ads_api": meta_ads_api,
        "platform_to_origen_map": {
            p.value: (o.value if o else None) for p, o in _PLATAFORMA_TO_ORIGEN.items()
        },
    })


@dashboard_bp.route("/actividad", methods=["GET"])
@require_role(["super_admin"])
def actividad_reciente():
    """Últimas 50 actividades del sistema."""
    from models import ActividadLog
    limit = min(int(request.args.get("limit", 50)), 200)
    logs = ActividadLog.query.order_by(ActividadLog.fecha.desc()).limit(limit).all()
    return jsonify([l.to_dict() for l in logs])


# ═══════════════════════════════════════════════════════════════════
# REVISIÓN COMERCIAL — vistas para junta de gerente con sus vendedores
# ═══════════════════════════════════════════════════════════════════


@dashboard_bp.route("/leads-por-origen", methods=["GET"])
def leads_por_origen():
    """Tabla embudo de conversión por canal de origen.
    Filtros: ?mes=YYYY-MM (default mes actual), ?marca=Aromatex (opcional)."""
    mes_param = request.args.get("mes")
    inicio_mes, fin_mes = _get_date_range(mes_param)
    marca = request.args.get("marca")

    etapas_calif = [
        EtapaPipeline.CONTACTO_1, EtapaPipeline.CONTACTO_2,
        EtapaPipeline.CONTACTO_3, EtapaPipeline.CONTACTO_4,
        EtapaPipeline.COTIZACION, EtapaPipeline.DEMO,
        EtapaPipeline.NEGOCIACION, EtapaPipeline.CIERRE_GANADO,
    ]
    etapas_cotiz = [
        EtapaPipeline.COTIZACION, EtapaPipeline.DEMO,
        EtapaPipeline.NEGOCIACION, EtapaPipeline.CIERRE_GANADO,
    ]

    leads_q = Lead.query.filter(
        Lead.fecha_creacion >= inicio_mes,
        Lead.fecha_creacion < fin_mes,
    )
    leads_q = _apply_vendedor_filter(leads_q)
    leads_q = _apply_un_filter(leads_q)
    if marca:
        leads_q = leads_q.filter(Lead.marca_interes == marca)

    # Counts por origen + etapas
    def counts_por_origen(query_filter=None):
        q = leads_q
        if query_filter is not None:
            q = q.filter(query_filter)
        rows = q.with_entities(Lead.origen, func.count(Lead.id)).group_by(Lead.origen).all()
        return {(r[0].value if r[0] else "Sin origen"): int(r[1]) for r in rows}

    total_map = counts_por_origen()
    calif_map = counts_por_origen(Lead.etapa_pipeline.in_(etapas_calif))
    cotiz_map = counts_por_origen(Lead.etapa_pipeline.in_(etapas_cotiz))
    ganados_map = counts_por_origen(Lead.etapa_pipeline == EtapaPipeline.CIERRE_GANADO)
    perdidos_map = counts_por_origen(Lead.etapa_pipeline == EtapaPipeline.CIERRE_PERDIDO)

    # Revenue ganado por origen
    rev_rows = (
        leads_q.filter(Lead.etapa_pipeline == EtapaPipeline.CIERRE_GANADO)
        .with_entities(
            Lead.origen,
            func.coalesce(func.sum(func.coalesce(
                Lead.factura_monto,
                Lead.cantidad_productos * Lead.precio_unitario,
                Lead.valor_estimado, 0,
            )), 0),
        ).group_by(Lead.origen).all()
    )
    revenue_map = {(r[0].value if r[0] else "Sin origen"): float(r[1] or 0) for r in rev_rows}

    # Valor del pipeline activo por origen (no ganados ni perdidos)
    pipe_rows = (
        leads_q.filter(Lead.etapa_pipeline.notin_([
            EtapaPipeline.CIERRE_GANADO, EtapaPipeline.CIERRE_PERDIDO,
        ]))
        .with_entities(
            Lead.origen,
            func.coalesce(func.sum(func.coalesce(
                Lead.factura_monto,
                Lead.cantidad_productos * Lead.precio_unitario,
                Lead.valor_estimado, 0,
            )), 0),
        ).group_by(Lead.origen).all()
    )
    pipe_map = {(r[0].value if r[0] else "Sin origen"): float(r[1] or 0) for r in pipe_rows}

    # Construir filas
    all_origenes = set(total_map) | set(calif_map) | set(cotiz_map) | set(ganados_map) | set(perdidos_map)
    filas = []
    for origen in all_origenes:
        total = total_map.get(origen, 0)
        if total == 0:
            continue
        ganados = ganados_map.get(origen, 0)
        perdidos = perdidos_map.get(origen, 0)
        filas.append({
            "origen": origen,
            "total": total,
            "calificados": calif_map.get(origen, 0),
            "cotizados":  cotiz_map.get(origen, 0),
            "ganados":    ganados,
            "perdidos":   perdidos,
            "en_proceso": total - ganados - perdidos,
            "revenue":    revenue_map.get(origen, 0.0),
            "pipe_activo": pipe_map.get(origen, 0.0),
            "tasa_cierre": round(ganados / total * 100, 1) if total > 0 else 0,
            "tasa_calificacion": round(calif_map.get(origen, 0) / total * 100, 1) if total > 0 else 0,
        })
    filas.sort(key=lambda f: -f["total"])

    # Totales
    total_all = sum(f["total"] for f in filas)
    ganados_all = sum(f["ganados"] for f in filas)
    return jsonify({
        "mes":           inicio_mes.strftime("%Y-%m"),
        "marca_filter":  marca,
        "filas":         filas,
        "total":         total_all,
        "total_ganados": ganados_all,
        "total_revenue": sum(f["revenue"] for f in filas),
        "total_pipe_activo": sum(f["pipe_activo"] for f in filas),
        "tasa_global":   round(ganados_all / total_all * 100, 1) if total_all > 0 else 0,
    })


def _kpis_vendedor(vendedor_usuario_id: str, inicio: date, fin: date) -> dict:
    """KPIs resumen del vendedor para la lista master de revisión."""
    base = Lead.query.filter(Lead.usuario_asignado_id == vendedor_usuario_id)
    valor_expr = func.coalesce(
        Lead.factura_monto,
        Lead.cantidad_productos * Lead.precio_unitario,
        Lead.valor_estimado, 0,
    )

    leads_mes = base.filter(
        Lead.fecha_creacion >= inicio, Lead.fecha_creacion < fin,
    ).count()
    ganados_mes = base.filter(
        Lead.fecha_creacion >= inicio, Lead.fecha_creacion < fin,
        Lead.etapa_pipeline == EtapaPipeline.CIERRE_GANADO,
    ).count()
    perdidos_mes = base.filter(
        Lead.fecha_creacion >= inicio, Lead.fecha_creacion < fin,
        Lead.etapa_pipeline == EtapaPipeline.CIERRE_PERDIDO,
    ).count()

    # Pipe activo (snapshot ahora, NO filtrado por mes)
    activos_q = base.filter(Lead.etapa_pipeline.notin_([
        EtapaPipeline.CIERRE_GANADO, EtapaPipeline.CIERRE_PERDIDO,
    ]))
    leads_activos = activos_q.count()
    pipe_activo = float(activos_q.with_entities(func.coalesce(func.sum(valor_expr), 0)).scalar() or 0)

    # Revenue ganado del mes
    revenue_mes = float(base.filter(
        Lead.fecha_creacion >= inicio, Lead.fecha_creacion < fin,
        Lead.etapa_pipeline == EtapaPipeline.CIERRE_GANADO,
    ).with_entities(func.coalesce(func.sum(valor_expr), 0)).scalar() or 0)

    def _type_filter(tipo):
        return Lead.tipo_venta == tipo if tipo else Lead.tipo_venta.is_(None)

    split_tipo_venta = {}
    for key, label in [
        ("recurrente", "Recurrente"),
        ("eventual", "Eventual"),
        ("sin_tipo", None),
    ]:
        tipo_base = base.filter(_type_filter(label))
        tipo_mes = tipo_base.filter(Lead.fecha_creacion >= inicio, Lead.fecha_creacion < fin)
        tipo_activos = tipo_base.filter(Lead.etapa_pipeline.notin_([
            EtapaPipeline.CIERRE_GANADO, EtapaPipeline.CIERRE_PERDIDO,
        ]))
        tipo_ganados_mes = tipo_mes.filter(Lead.etapa_pipeline == EtapaPipeline.CIERRE_GANADO).count()
        tipo_revenue_mes = float(tipo_mes.filter(
            Lead.etapa_pipeline == EtapaPipeline.CIERRE_GANADO,
        ).with_entities(func.coalesce(func.sum(valor_expr), 0)).scalar() or 0)
        tipo_leads_mes = tipo_mes.count()
        split_tipo_venta[key] = {
            "label": label or "Sin tipo",
            "leads_mes": tipo_leads_mes,
            "leads_activos": tipo_activos.count(),
            "pipe_activo": float(tipo_activos.with_entities(
                func.coalesce(func.sum(valor_expr), 0),
            ).scalar() or 0),
            "ganados_mes": tipo_ganados_mes,
            "revenue_mes": tipo_revenue_mes,
            "tasa_cierre": round(tipo_ganados_mes / tipo_leads_mes * 100, 1) if tipo_leads_mes > 0 else 0,
        }

    return {
        "leads_mes":       leads_mes,
        "ganados_mes":     ganados_mes,
        "perdidos_mes":    perdidos_mes,
        "leads_activos":   leads_activos,
        "pipe_activo":     pipe_activo,
        "revenue_mes":     revenue_mes,
        "tasa_cierre":     round(ganados_mes / leads_mes * 100, 1) if leads_mes > 0 else 0,
        "split_tipo_venta": split_tipo_venta,
    }


@dashboard_bp.route("/vendedores-tabla", methods=["GET"])
@require_role(["super_admin"])
def vendedores_tabla():
    """Tabla embudo de conversión por VENDEDOR (mismo shape que leads-por-origen
    pero agrupado por usuario_asignado_id). Para comparar vendedores en la junta.
    """
    from models import Usuario
    mes_param = request.args.get("mes")
    inicio_mes, fin_mes = _get_date_range(mes_param)
    marca = request.args.get("marca")

    etapas_calif = [
        EtapaPipeline.CONTACTO_1, EtapaPipeline.CONTACTO_2,
        EtapaPipeline.CONTACTO_3, EtapaPipeline.CONTACTO_4,
        EtapaPipeline.COTIZACION, EtapaPipeline.DEMO,
        EtapaPipeline.NEGOCIACION, EtapaPipeline.CIERRE_GANADO,
    ]
    etapas_cotiz = [
        EtapaPipeline.COTIZACION, EtapaPipeline.DEMO,
        EtapaPipeline.NEGOCIACION, EtapaPipeline.CIERRE_GANADO,
    ]

    leads_q = Lead.query.filter(
        Lead.fecha_creacion >= inicio_mes,
        Lead.fecha_creacion < fin_mes,
        Lead.usuario_asignado_id.isnot(None),
    )
    if marca:
        leads_q = leads_q.filter(Lead.marca_interes == marca)

    def counts_por_vendedor(query_filter=None):
        q = leads_q
        if query_filter is not None:
            q = q.filter(query_filter)
        rows = q.with_entities(Lead.usuario_asignado_id, func.count(Lead.id)).group_by(Lead.usuario_asignado_id).all()
        return {str(r[0]): int(r[1]) for r in rows if r[0]}

    total_map = counts_por_vendedor()
    calif_map = counts_por_vendedor(Lead.etapa_pipeline.in_(etapas_calif))
    cotiz_map = counts_por_vendedor(Lead.etapa_pipeline.in_(etapas_cotiz))
    ganados_map = counts_por_vendedor(Lead.etapa_pipeline == EtapaPipeline.CIERRE_GANADO)
    perdidos_map = counts_por_vendedor(Lead.etapa_pipeline == EtapaPipeline.CIERRE_PERDIDO)

    # Revenue ganado por vendedor
    rev_rows = (
        leads_q.filter(Lead.etapa_pipeline == EtapaPipeline.CIERRE_GANADO)
        .with_entities(
            Lead.usuario_asignado_id,
            func.coalesce(func.sum(func.coalesce(
                Lead.factura_monto,
                Lead.cantidad_productos * Lead.precio_unitario,
                Lead.valor_estimado, 0,
            )), 0),
        ).group_by(Lead.usuario_asignado_id).all()
    )
    revenue_map = {str(r[0]): float(r[1] or 0) for r in rev_rows if r[0]}

    # Pipe activo (snapshot ACTUAL, NO filtrado por mes) — esto es lo que
    # tienen abierto hoy independientemente de cuándo entró.
    pipe_rows = (
        Lead.query
        .filter(
            Lead.usuario_asignado_id.isnot(None),
            Lead.etapa_pipeline.notin_([
                EtapaPipeline.CIERRE_GANADO, EtapaPipeline.CIERRE_PERDIDO,
            ]),
        )
    )
    if marca:
        pipe_rows = pipe_rows.filter(Lead.marca_interes == marca)
    pipe_rows = pipe_rows.with_entities(
        Lead.usuario_asignado_id,
        func.coalesce(func.sum(func.coalesce(
            Lead.factura_monto,
            Lead.cantidad_productos * Lead.precio_unitario,
            Lead.valor_estimado, 0,
        )), 0),
        func.count(Lead.id),
    ).group_by(Lead.usuario_asignado_id).all()
    pipe_valor_map = {str(r[0]): float(r[1] or 0) for r in pipe_rows if r[0]}
    pipe_count_map = {str(r[0]): int(r[2]) for r in pipe_rows if r[0]}

    # Nombres de vendedores
    vendedor_ids = set(total_map) | set(pipe_valor_map)
    vendedores = {
        str(v.id): v for v in Usuario.query.filter(Usuario.id.in_(vendedor_ids)).all()
    } if vendedor_ids else {}

    filas = []
    for vid, v in vendedores.items():
        total = total_map.get(vid, 0)
        ganados = ganados_map.get(vid, 0)
        perdidos = perdidos_map.get(vid, 0)
        if total == 0 and pipe_count_map.get(vid, 0) == 0:
            continue
        filas.append({
            "vendedor_id":   vid,
            "vendedor":      v.nombre,
            "marcas":        list(v.especialidad_marca or []),
            "total":         total,
            "calificados":   calif_map.get(vid, 0),
            "cotizados":     cotiz_map.get(vid, 0),
            "ganados":       ganados,
            "perdidos":      perdidos,
            "en_proceso":    total - ganados - perdidos,
            "revenue":       revenue_map.get(vid, 0.0),
            "pipe_activo":   pipe_valor_map.get(vid, 0.0),
            "pipe_count":    pipe_count_map.get(vid, 0),
            "tasa_cierre":   round(ganados / total * 100, 1) if total > 0 else 0,
            "tasa_calificacion": round(calif_map.get(vid, 0) / total * 100, 1) if total > 0 else 0,
        })
    filas.sort(key=lambda f: -f["pipe_activo"])

    total_all = sum(f["total"] for f in filas)
    ganados_all = sum(f["ganados"] for f in filas)
    return jsonify({
        "mes":               inicio_mes.strftime("%Y-%m"),
        "marca_filter":      marca,
        "filas":             filas,
        "total":             total_all,
        "total_ganados":     ganados_all,
        "total_revenue":     sum(f["revenue"] for f in filas),
        "total_pipe_activo": sum(f["pipe_activo"] for f in filas),
        "tasa_global":       round(ganados_all / total_all * 100, 1) if total_all > 0 else 0,
    })


@dashboard_bp.route("/ventas-reporte.csv", methods=["GET"])
@require_role(["super_admin"])
def ventas_reporte_csv():
    """Descarga ventas ganadas del periodo, una fila por lead_id.

    Fuente actual: Lead en Cerrado Ganado. La tabla sales existe, pero hoy no
    tiene datos históricos; por eso el reporte operativo se basa en leads y usa
    lead_id como llave de venta para no duplicar por teléfono/cliente.
    """
    from un_filter import normalizar_un
    from models import Usuario

    mes_param = request.args.get("mes")
    inicio, fin = _get_date_range(mes_param)
    un_filter = normalizar_un(request.args.get("un"))
    vendedor_id = request.args.get("vendedor_id")

    q = (
        Lead.query
        .filter(Lead.etapa_pipeline == EtapaPipeline.CIERRE_GANADO)
        .order_by(Lead.factura_fecha.desc().nullslast(), Lead.fecha_actualizacion.desc())
    )
    if vendedor_id:
        q = q.filter(Lead.usuario_asignado_id == vendedor_id)

    rows = []
    seen_leads = set()
    vendedor_ids = set()
    for lead in q.all():
        lead_key = str(lead.id)
        if lead_key in seen_leads:
            continue
        seen_leads.add(lead_key)

        un_canonica = normalizar_un(lead.marca_interes) or "Sin UN"
        if un_filter and un_canonica != un_filter:
            continue

        fecha_venta = (
            lead.factura_fecha
            or (lead.factura_registrada_at.date() if lead.factura_registrada_at else None)
            or (lead.fecha_actualizacion.date() if lead.fecha_actualizacion else None)
        )
        if not fecha_venta or fecha_venta < inicio or fecha_venta >= fin:
            continue

        if lead.usuario_asignado_id:
            vendedor_ids.add(lead.usuario_asignado_id)
        rows.append((lead, fecha_venta, un_canonica))

    vendedores = {
        str(v.id): v.nombre
        for v in Usuario.query.filter(Usuario.id.in_(vendedor_ids)).all()
    } if vendedor_ids else {}

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "lead_id",
        "fecha_venta",
        "vendedor",
        "vendedor_id",
        "un_canonica",
        "un_original",
        "cliente",
        "empresa",
        "telefono",
        "origen",
        "tipo_venta",
        "monto_facturado",
        "valor_estimado",
        "valor_reporte",
        "factura_numero",
        "factura_fecha",
        "factura_registrada_at",
        "fecha_creacion_lead",
        "fecha_actualizacion_lead",
        "notas_factura",
    ])
    for lead, fecha_venta, un_canonica in rows:
        valor_estimado = float(lead.valor_estimado or 0)
        monto_facturado = float(lead.factura_monto or 0)
        valor_reporte = float(lead.valor_calculado or 0)
        vendedor_key = str(lead.usuario_asignado_id) if lead.usuario_asignado_id else ""
        writer.writerow([
            str(lead.id),
            fecha_venta.isoformat(),
            vendedores.get(vendedor_key, "Sin vendedor"),
            vendedor_key,
            un_canonica,
            lead.marca_interes or "",
            lead.nombre or "",
            lead.empresa_nombre or "",
            lead.telefono or "",
            lead.origen.value if lead.origen else "",
            lead.tipo_venta or "",
            f"{monto_facturado:.2f}",
            f"{valor_estimado:.2f}",
            f"{valor_reporte:.2f}",
            lead.factura_numero or "",
            lead.factura_fecha.isoformat() if lead.factura_fecha else "",
            lead.factura_registrada_at.isoformat() if lead.factura_registrada_at else "",
            lead.fecha_creacion.isoformat() if lead.fecha_creacion else "",
            lead.fecha_actualizacion.isoformat() if lead.fecha_actualizacion else "",
            lead.factura_notas or "",
        ])

    label_un = un_filter or "todas"
    filename = f"reporte_ventas_{inicio.strftime('%Y-%m')}_{label_un}.csv"
    csv_data = "\ufeff" + output.getvalue()
    return Response(
        csv_data,
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@dashboard_bp.route("/vendedores-review", methods=["GET"])
@require_role(["super_admin"])
def vendedores_review():
    """Lista de vendedores con KPIs resumen para la vista master de revisión.
    FEAT-2026-06-29: filtro global ?un= por especialidad_marca."""
    from models import Usuario
    from un_filter import usuario_pertenece_a_un
    mes_param = request.args.get("mes")
    inicio, fin = _get_date_range(mes_param)
    un = request.args.get("un")

    vendedores_all = (
        Usuario.query
        .filter(Usuario.en_turno.is_(True))
        .order_by(Usuario.nombre.asc()).all()
    )
    if un:
        vendedores = [v for v in vendedores_all
                      if usuario_pertenece_a_un(v.especialidad_marca, un)]
    else:
        vendedores = vendedores_all
    out = []
    for v in vendedores:
        kpis = _kpis_vendedor(v.id, inicio, fin)
        # Solo incluir si tiene actividad (leads activos o leads del mes)
        if kpis["leads_activos"] == 0 and kpis["leads_mes"] == 0:
            continue
        out.append({
            "vendedor_id": str(v.id),
            "nombre":      v.nombre,
            "marcas":      list(v.especialidad_marca or []),
            **kpis,
        })
    out.sort(key=lambda x: -x["pipe_activo"])
    return jsonify({"mes": inicio.strftime("%Y-%m"), "vendedores": out})


@dashboard_bp.route("/vendedor-review/<uuid:vendedor_id>", methods=["GET"])
@require_role(["super_admin"])
def vendedor_review(vendedor_id):
    """Drill-down completo de UN vendedor para la junta de revisión.
    - KPIs del mes
    - Funnel (counts + valor por etapa)
    - Lista de leads agrupada por etapa con días en etapa, último contacto, valor
    """
    from models import Usuario
    from datetime import datetime as _dt, timezone as _tz
    mes_param = request.args.get("mes")
    inicio, fin = _get_date_range(mes_param)

    v = Usuario.query.get(str(vendedor_id))
    if not v:
        return jsonify({"error": "Vendedor no encontrado"}), 404
    kpis = _kpis_vendedor(v.id, inicio, fin)

    # Funnel: counts + valor en cada etapa (snapshot leads activos + cierres del mes)
    etapas_orden = [
        EtapaPipeline.NUEVO_LEAD, EtapaPipeline.CONTACTO_1, EtapaPipeline.CONTACTO_2,
        EtapaPipeline.CONTACTO_3, EtapaPipeline.CONTACTO_4, EtapaPipeline.PRESENTACION,
        EtapaPipeline.COTIZACION, EtapaPipeline.DEMO, EtapaPipeline.NEGOCIACION,
        EtapaPipeline.CIERRE_GANADO, EtapaPipeline.CIERRE_PERDIDO,
    ]
    funnel = []
    for etapa in etapas_orden:
        n = Lead.query.filter(
            Lead.usuario_asignado_id == v.id,
            Lead.etapa_pipeline == etapa,
        ).count()
        valor = float(Lead.query.filter(
            Lead.usuario_asignado_id == v.id,
            Lead.etapa_pipeline == etapa,
        ).with_entities(func.coalesce(func.sum(func.coalesce(
            Lead.factura_monto,
            Lead.cantidad_productos * Lead.precio_unitario,
            Lead.valor_estimado, 0,
        )), 0)).scalar() or 0)
        funnel.append({"etapa": etapa.value, "count": n, "valor": valor})

    # Lista de leads agrupada por etapa (solo activos: sin cerrados)
    leads = (
        Lead.query
        .filter(
            Lead.usuario_asignado_id == v.id,
            Lead.etapa_pipeline.notin_([EtapaPipeline.CIERRE_GANADO, EtapaPipeline.CIERRE_PERDIDO]),
        )
        .order_by(Lead.fecha_ultimo_contacto.asc().nullsfirst(), Lead.fecha_creacion.asc())
        .all()
    )
    now = _dt.now(_tz.utc)
    leads_por_etapa = {}
    for l in leads:
        # Días desde fecha_ultimo_contacto (o creación si nunca contactó)
        ref = l.fecha_ultimo_contacto or l.fecha_creacion
        if ref and ref.tzinfo is None:
            ref = ref.replace(tzinfo=_tz.utc)
        dias = (now - ref).days if ref else 0
        # Días en la etapa actual (usar fecha_actualizacion como proxy)
        ref2 = l.fecha_actualizacion or l.fecha_creacion
        if ref2 and ref2.tzinfo is None:
            ref2 = ref2.replace(tzinfo=_tz.utc)
        dias_etapa = (now - ref2).days if ref2 else 0

        valor = float(l.valor_calculado or 0)
        etapa = l.etapa_pipeline.value
        leads_por_etapa.setdefault(etapa, []).append({
            "id":               str(l.id),
            "nombre":           l.nombre,
            "telefono":         l.telefono,
            "empresa":          l.empresa_nombre,
            "marca_interes":    l.marca_interes,
            "origen":           l.origen.value if l.origen else None,
            "valor":            valor,
            "estado":           l.estado_cliente,
            "tipo_cliente":     l.tipo_cliente,
            "tipo_venta":       l.tipo_venta,  # FEAT-2026-07-06: warning "sin clasificar"
            "icp_nivel":        l.icp_nivel,
            "dias_sin_contacto": dias,
            "dias_en_etapa":    dias_etapa,
            "fecha_ultimo_contacto": l.fecha_ultimo_contacto.isoformat() if l.fecha_ultimo_contacto else None,
            "proximo_contacto": l.proximo_contacto.isoformat() if l.proximo_contacto else None,
            "respondio":        l.respondio_ultimo_contacto,
            "meta_campaign_nombre": (l.meta_campaign_info or {}).get("nombre") if l.meta_campaign else None,
            "stuck":            dias_etapa >= 7,
        })

    return jsonify({
        "mes":               inicio.strftime("%Y-%m"),
        "vendedor_id":       str(v.id),
        "vendedor_nombre":   v.nombre,
        "marcas":            list(v.especialidad_marca or []),
        "kpis":              kpis,
        "funnel":            funnel,
        "leads_por_etapa":   leads_por_etapa,
    })

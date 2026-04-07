# blueprints/dashboard.py
"""
API para metricas del dashboard.
Filtra automaticamente por vendedor si el usuario logueado es vendedor.
"""
from datetime import date
from flask import Blueprint, request, jsonify, session
from sqlalchemy import func
from extensions import db
from models import Lead, EtapaPipeline, GastoPublicidad, PlataformaAds
from blueprints.auth import get_vendedor_filter, require_role

dashboard_bp = Blueprint("dashboard", __name__)


def _apply_vendedor_filter(query):
    """Filtra query de leads por vendedor si no es super_admin."""
    vid = get_vendedor_filter()
    if vid:
        query = query.filter(Lead.usuario_asignado_id == vid)
    return query


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


@dashboard_bp.route("/pipeline-valores", methods=["GET"])
def pipeline_valores():
    q = db.session.query(
        Lead.etapa_pipeline,
        func.count(Lead.id).label("cantidad"),
        func.coalesce(func.sum(func.coalesce(
            Lead.cantidad_productos * Lead.precio_unitario,
            Lead.valor_estimado, 0,
        )), 0).label("valor_total"),
    )

    vid = get_vendedor_filter()
    if vid:
        q = q.filter(Lead.usuario_asignado_id == vid)

    resultados = q.group_by(Lead.etapa_pipeline).all()

    data = {}
    for etapa_enum, cantidad, valor in resultados:
        data[etapa_enum.value] = {"cantidad": cantidad, "valor": float(valor)}

    for etapa in EtapaPipeline:
        if etapa.value not in data:
            data[etapa.value] = {"cantidad": 0, "valor": 0}

    return jsonify(data)


@dashboard_bp.route("/embudo", methods=["GET"])
def embudo():
    mes_param = request.args.get("mes")
    inicio_mes, fin_mes = _get_date_range(mes_param)

    leads_q = Lead.query.filter(
        Lead.fecha_creacion >= inicio_mes,
        Lead.fecha_creacion < fin_mes,
    )
    leads_q = _apply_vendedor_filter(leads_q)

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

    # Revenue
    rev_q = db.session.query(func.coalesce(func.sum(func.coalesce(
        Lead.cantidad_productos * Lead.precio_unitario, Lead.valor_estimado, 0,
    )), 0)).filter(
        Lead.fecha_creacion >= inicio_mes, Lead.fecha_creacion < fin_mes,
        Lead.etapa_pipeline == EtapaPipeline.CIERRE_GANADO,
    )
    vid = get_vendedor_filter()
    if vid:
        rev_q = rev_q.filter(Lead.usuario_asignado_id == vid)
    revenue = float(rev_q.scalar() or 0)

    # Pipe total
    pipe_q = db.session.query(func.coalesce(func.sum(func.coalesce(
        Lead.cantidad_productos * Lead.precio_unitario, Lead.valor_estimado, 0,
    )), 0)).filter(
        Lead.fecha_creacion >= inicio_mes, Lead.fecha_creacion < fin_mes,
        Lead.etapa_pipeline.notin_([EtapaPipeline.CIERRE_PERDIDO]),
    )
    if vid:
        pipe_q = pipe_q.filter(Lead.usuario_asignado_id == vid)
    pipe_total = float(pipe_q.scalar() or 0)

    # Gastos (solo super_admin ve gastos reales, vendedor ve 0)
    gasto_ads = 0.0
    if not vid:
        gasto_row = db.session.query(func.coalesce(func.sum(GastoPublicidad.monto), 0)).filter(
            GastoPublicidad.fecha >= inicio_mes, GastoPublicidad.fecha < fin_mes,
        ).scalar()
        gasto_ads = float(gasto_row or 0)

    costo_por_lead = round(gasto_ads / total, 2) if total > 0 else 0
    costo_por_cierre = round(gasto_ads / ganados, 2) if ganados > 0 else 0
    roi = round(revenue / gasto_ads, 2) if gasto_ads > 0 else 0

    return jsonify({
        "mes": inicio_mes.strftime("%Y-%m"),
        "leads_totales": total,
        "calificados": calificados,
        "cotizados": cotizados,
        "ganados": ganados,
        "perdidos": perdidos,
        "revenue_ganado": revenue,
        "pipe_total": pipe_total,
        "gasto_ads": gasto_ads,
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

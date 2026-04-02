# blueprints/dashboard.py
"""
API para metricas del dashboard:
  - Embudo de conversion
  - Valor del pipe por etapa
  - CRUD de gastos de publicidad
"""
from datetime import date, timedelta
from flask import Blueprint, request, jsonify
from sqlalchemy import func, case
from extensions import db
from models import (
    Lead, EtapaPipeline, GastoPublicidad, PlataformaAds,
)

dashboard_bp = Blueprint("dashboard", __name__)


# ──────────────────────────────────────────────
# Metricas del pipeline (valor por etapa)
# ──────────────────────────────────────────────
@dashboard_bp.route("/pipeline-valores", methods=["GET"])
def pipeline_valores():
    """
    Retorna el valor acumulado por cada etapa del pipeline.
    Respuesta: { "Nuevo Lead": 45000, "Calificando": 120000, ... }
    """
    resultados = (
        db.session.query(
            Lead.etapa_pipeline,
            func.count(Lead.id).label("cantidad"),
            func.coalesce(
                func.sum(
                    func.coalesce(
                        Lead.cantidad_productos * Lead.precio_unitario,
                        Lead.valor_estimado,
                        0,
                    )
                ),
                0,
            ).label("valor_total"),
        )
        .group_by(Lead.etapa_pipeline)
        .all()
    )

    data = {}
    for etapa_enum, cantidad, valor in resultados:
        data[etapa_enum.value] = {
            "cantidad": cantidad,
            "valor": float(valor),
        }

    # Rellenar etapas sin leads
    for etapa in EtapaPipeline:
        if etapa.value not in data:
            data[etapa.value] = {"cantidad": 0, "valor": 0}

    return jsonify(data)


# ──────────────────────────────────────────────
# Embudo de conversion
# ──────────────────────────────────────────────
@dashboard_bp.route("/embudo", methods=["GET"])
def embudo():
    """
    Embudo de conversion del mes actual (o mes especificado).
    Query params: ?mes=2026-04 (opcional, default mes actual)

    Retorna:
    {
      "mes": "2026-04",
      "leads_totales": 50,
      "calificados": 30,
      "cotizados": 15,
      "ganados": 5,
      "perdidos": 8,
      "revenue_ganado": 250000,
      "pipe_total": 890000,
      "gasto_ads": 45000,
      "costo_por_lead": 900,
      "costo_por_cierre": 9000,
      "roi": 4.56
    }
    """
    mes_param = request.args.get("mes")
    if mes_param:
        year, month = mes_param.split("-")
        inicio_mes = date(int(year), int(month), 1)
    else:
        hoy = date.today()
        inicio_mes = hoy.replace(day=1)

    if inicio_mes.month == 12:
        fin_mes = inicio_mes.replace(year=inicio_mes.year + 1, month=1)
    else:
        fin_mes = inicio_mes.replace(month=inicio_mes.month + 1)

    # Leads del periodo
    leads_q = Lead.query.filter(
        Lead.fecha_creacion >= inicio_mes,
        Lead.fecha_creacion < fin_mes,
    )

    total = leads_q.count()

    etapas_calificadas = [
        EtapaPipeline.CALIFICANDO,
        EtapaPipeline.PRESENTACION_COTIZACION,
        EtapaPipeline.SEGUIMIENTO,
        EtapaPipeline.CIERRE_GANADO,
    ]
    etapas_cotizadas = [
        EtapaPipeline.PRESENTACION_COTIZACION,
        EtapaPipeline.SEGUIMIENTO,
        EtapaPipeline.CIERRE_GANADO,
    ]

    calificados = leads_q.filter(Lead.etapa_pipeline.in_(etapas_calificadas)).count()
    cotizados = leads_q.filter(Lead.etapa_pipeline.in_(etapas_cotizadas)).count()
    ganados = leads_q.filter(Lead.etapa_pipeline == EtapaPipeline.CIERRE_GANADO).count()
    perdidos = leads_q.filter(Lead.etapa_pipeline == EtapaPipeline.CIERRE_PERDIDO).count()

    # Revenue de cierres ganados
    revenue_row = (
        db.session.query(
            func.coalesce(
                func.sum(
                    func.coalesce(
                        Lead.cantidad_productos * Lead.precio_unitario,
                        Lead.valor_estimado,
                        0,
                    )
                ),
                0,
            )
        )
        .filter(
            Lead.fecha_creacion >= inicio_mes,
            Lead.fecha_creacion < fin_mes,
            Lead.etapa_pipeline == EtapaPipeline.CIERRE_GANADO,
        )
        .scalar()
    )
    revenue = float(revenue_row or 0)

    # Pipe total (todas las etapas activas)
    pipe_row = (
        db.session.query(
            func.coalesce(
                func.sum(
                    func.coalesce(
                        Lead.cantidad_productos * Lead.precio_unitario,
                        Lead.valor_estimado,
                        0,
                    )
                ),
                0,
            )
        )
        .filter(
            Lead.fecha_creacion >= inicio_mes,
            Lead.fecha_creacion < fin_mes,
            Lead.etapa_pipeline.notin_([EtapaPipeline.CIERRE_PERDIDO]),
        )
        .scalar()
    )
    pipe_total = float(pipe_row or 0)

    # Gasto en ads del mes
    gasto_row = (
        db.session.query(func.coalesce(func.sum(GastoPublicidad.monto), 0))
        .filter(
            GastoPublicidad.fecha >= inicio_mes,
            GastoPublicidad.fecha < fin_mes,
        )
        .scalar()
    )
    gasto_ads = float(gasto_row or 0)

    # Metricas derivadas
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
# CRUD de gastos de publicidad
# ──────────────────────────────────────────────
@dashboard_bp.route("/gastos", methods=["GET"])
def listar_gastos():
    """Lista gastos, opcionalmente filtrados por mes y marca."""
    mes_param = request.args.get("mes")
    marca = request.args.get("marca")

    q = GastoPublicidad.query

    if mes_param:
        year, month = mes_param.split("-")
        inicio = date(int(year), int(month), 1)
        if inicio.month == 12:
            fin = inicio.replace(year=inicio.year + 1, month=1)
        else:
            fin = inicio.replace(month=inicio.month + 1)
        q = q.filter(GastoPublicidad.fecha >= inicio, GastoPublicidad.fecha < fin)

    if marca:
        q = q.filter(GastoPublicidad.marca == marca)

    gastos = q.order_by(GastoPublicidad.fecha.desc()).all()
    return jsonify([g.to_dict() for g in gastos])


@dashboard_bp.route("/gastos", methods=["POST"])
def registrar_gasto():
    """
    Registra un gasto de publicidad.
    Body: {
        "plataforma": "Facebook",
        "marca": "Weldex",
        "campana": "Campaña Soldadores Abril",
        "monto": 15000,
        "fecha": "2026-04-01",
        "notas": "Segmentacion: industriales norte"
    }
    """
    data = request.get_json() or {}

    try:
        plataforma = PlataformaAds(data["plataforma"])
    except (ValueError, KeyError):
        opciones = [p.value for p in PlataformaAds]
        return jsonify({"error": f"Plataforma invalida. Opciones: {opciones}"}), 400

    gasto = GastoPublicidad(
        plataforma=plataforma,
        marca=data.get("marca"),
        campana=data.get("campana"),
        monto=data["monto"],
        fecha=date.fromisoformat(data["fecha"]),
        notas=data.get("notas"),
    )
    db.session.add(gasto)
    db.session.commit()

    return jsonify(gasto.to_dict()), 201


@dashboard_bp.route("/gastos/<uuid:gasto_id>", methods=["DELETE"])
def eliminar_gasto(gasto_id):
    gasto = db.session.get(GastoPublicidad, gasto_id)
    if not gasto:
        return jsonify({"error": "Gasto no encontrado"}), 404
    db.session.delete(gasto)
    db.session.commit()
    return jsonify({"status": "eliminado"}), 200

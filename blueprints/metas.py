# blueprints/metas.py
"""
Metas mensuales por vendedor.
- Super Admin: CRUD + resumen del equipo
- Vendedor: solo lectura de su propio progreso
"""
from datetime import date
from flask import Blueprint, request, jsonify, session
from sqlalchemy import func
from extensions import db
from models import MetaVendedor, Usuario, Lead, EtapaPipeline
from blueprints.auth import require_role, get_vendedor_filter

metas_bp = Blueprint("metas", __name__)


def _mes_actual():
    return date.today().strftime("%Y-%m")


def _calcular_ventas(usuario_id, mes):
    """Revenue cerrado ganado de un vendedor en un mes."""
    year, month = mes.split("-")
    inicio = date(int(year), int(month), 1)
    if inicio.month == 12:
        fin = inicio.replace(year=inicio.year + 1, month=1)
    else:
        fin = inicio.replace(month=inicio.month + 1)

    row = db.session.query(
        func.coalesce(func.sum(func.coalesce(
            Lead.cantidad_productos * Lead.precio_unitario,
            Lead.valor_estimado, 0,
        )), 0)
    ).filter(
        Lead.usuario_asignado_id == usuario_id,
        Lead.etapa_pipeline == EtapaPipeline.CIERRE_GANADO,
        Lead.fecha_creacion >= inicio,
        Lead.fecha_creacion < fin,
    ).scalar()

    return float(row or 0)


@metas_bp.route("/", methods=["GET"])
@require_role(["super_admin"])
def listar_metas():
    """Super Admin: todas las metas del mes (o mes especificado)."""
    mes = request.args.get("mes", _mes_actual())
    metas = MetaVendedor.query.filter_by(mes=mes).all()
    return jsonify([m.to_dict() for m in metas])


@metas_bp.route("/", methods=["POST"])
@require_role(["super_admin"])
def crear_o_actualizar_meta():
    """
    Super Admin: crear o actualizar meta.
    Body: { "usuario_id": "uuid", "mes": "2026-04", "meta_mxn": 50000 }
    """
    data = request.get_json() or {}
    usuario_id = data.get("usuario_id")
    mes = data.get("mes", _mes_actual())
    meta_mxn = data.get("meta_mxn")

    if not usuario_id or meta_mxn is None:
        return jsonify({"error": "usuario_id y meta_mxn requeridos"}), 400

    # Upsert
    meta = MetaVendedor.query.filter_by(usuario_id=usuario_id, mes=mes).first()
    if meta:
        meta.meta_mxn = meta_mxn
    else:
        meta = MetaVendedor(
            usuario_id=usuario_id,
            mes=mes,
            meta_mxn=meta_mxn,
            created_by=session.get("user_id"),
        )
        db.session.add(meta)

    db.session.commit()
    return jsonify(meta.to_dict()), 201


@metas_bp.route("/mi-progreso", methods=["GET"])
def mi_progreso():
    """Vendedor: su meta + ventas del mes actual."""
    usuario_id = session.get("usuario_id")
    if not usuario_id:
        return jsonify({"error": "Sin vendedor vinculado"}), 400

    mes = request.args.get("mes", _mes_actual())
    meta = MetaVendedor.query.filter_by(usuario_id=usuario_id, mes=mes).first()
    ventas = _calcular_ventas(usuario_id, mes)

    meta_mxn = float(meta.meta_mxn) if meta else 0
    pct = round((ventas / meta_mxn) * 100, 1) if meta_mxn > 0 else 0

    return jsonify({
        "usuario_id": usuario_id,
        "mes": mes,
        "meta_mxn": meta_mxn,
        "ventas_actual": ventas,
        "porcentaje": pct,
        "tiene_meta": meta is not None,
    })


@metas_bp.route("/resumen-equipo", methods=["GET"])
@require_role(["super_admin"])
def resumen_equipo():
    """Super Admin: tabla comparativa de todos los vendedores vs meta."""
    mes = request.args.get("mes", _mes_actual())

    vendedores = Usuario.query.filter(Usuario.en_turno.is_(True)).order_by(Usuario.nombre).all()
    resultado = []

    for v in vendedores:
        meta = MetaVendedor.query.filter_by(usuario_id=v.id, mes=mes).first()
        ventas = _calcular_ventas(v.id, mes)
        meta_mxn = float(meta.meta_mxn) if meta else 0
        pct = round((ventas / meta_mxn) * 100, 1) if meta_mxn > 0 else 0

        # Leads activos (no cerrados)
        leads_activos = Lead.query.filter(
            Lead.usuario_asignado_id == v.id,
            Lead.etapa_pipeline.notin_([EtapaPipeline.CIERRE_GANADO, EtapaPipeline.CIERRE_PERDIDO]),
        ).count()

        resultado.append({
            "usuario_id": str(v.id),
            "nombre": v.nombre,
            "meta_mxn": meta_mxn,
            "ventas_actual": ventas,
            "porcentaje": pct,
            "leads_activos": leads_activos,
            "tiene_meta": meta is not None,
        })

    return jsonify(resultado)

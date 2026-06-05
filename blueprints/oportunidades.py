# blueprints/oportunidades.py
"""
Oportunidades (Deals) — entidad pre-cierre. Reemplaza Zoho Deals para que
leads-manager pueda operar sin Zoho.

Endpoints bajo /api/oportunidades/.
"""
from datetime import datetime, timezone
from decimal import Decimal
from flask import Blueprint, request, jsonify, session
from sqlalchemy import func, or_

from extensions import db
from models import (
    Oportunidad, EtapaOportunidad, PROBABILIDAD_OPORTUNIDAD,
    Lead, EtapaPipeline, Usuario,
)

oportunidades_bp = Blueprint("oportunidades", __name__)


# ── Helpers ────────────────────────────────────────────────────────


def _parse_date(s):
    if not s:
        return None
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _parse_etapa(s):
    if not s:
        return None
    try:
        return EtapaOportunidad(s)
    except ValueError:
        return None


def _current_user_id():
    return session.get("usuario_id") or session.get("user_id")


def _to_decimal(v):
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v))
    except (ValueError, TypeError):
        return None


# ── List + filters ────────────────────────────────────────────────


@oportunidades_bp.route("/", methods=["GET"])
def list_oportunidades():
    """Filtros: ?etapa=&marca=&propietario=&search="""
    q = Oportunidad.query
    etapa = _parse_etapa(request.args.get("etapa"))
    marca = request.args.get("marca")
    propietario = request.args.get("propietario")
    search = (request.args.get("search") or "").strip()

    if etapa:
        q = q.filter(Oportunidad.etapa == etapa)
    if marca:
        q = q.filter(Oportunidad.marca_interes == marca)
    if propietario:
        q = q.filter(Oportunidad.propietario_id == propietario)
    if search:
        like = f"%{search}%"
        q = q.filter(or_(
            Oportunidad.nombre.ilike(like),
            Oportunidad.empresa.ilike(like),
            Oportunidad.contacto_nombre.ilike(like),
        ))

    rows = q.order_by(Oportunidad.fecha_actualizacion.desc()).all()
    return jsonify([r.to_dict() for r in rows])


@oportunidades_bp.route("/<uuid:opp_id>", methods=["GET"])
def get_oportunidad(opp_id):
    op = db.session.get(Oportunidad, opp_id)
    if not op:
        return jsonify({"error": "Oportunidad no encontrada"}), 404
    return jsonify(op.to_dict())


# ── Kanban view: agrupado por etapa ───────────────────────────────


@oportunidades_bp.route("/kanban", methods=["GET"])
def kanban():
    """Devuelve oportunidades agrupadas por etapa para vista Kanban.
    Filtros opcionales: ?marca=&propietario="""
    marca = request.args.get("marca")
    propietario = request.args.get("propietario")
    q = Oportunidad.query
    if marca:
        q = q.filter(Oportunidad.marca_interes == marca)
    if propietario:
        q = q.filter(Oportunidad.propietario_id == propietario)
    rows = q.order_by(Oportunidad.fecha_actualizacion.desc()).all()

    grouped = {}
    for et in EtapaOportunidad:
        grouped[et.value] = {
            "etapa": et.value,
            "probabilidad_default": PROBABILIDAD_OPORTUNIDAD.get(et, 0),
            "items": [],
            "valor_total": 0.0,
            "valor_ponderado_total": 0.0,
            "count": 0,
        }
    for op in rows:
        key = op.etapa.value if op.etapa else None
        if key and key in grouped:
            d = op.to_dict()
            grouped[key]["items"].append(d)
            grouped[key]["valor_total"] += d["valor"]
            grouped[key]["valor_ponderado_total"] += d["valor_ponderado"]
            grouped[key]["count"] += 1

    # Sumario global
    abiertas = [op for op in rows if op.etapa not in
                (EtapaOportunidad.CIERRE_GANADO, EtapaOportunidad.CIERRE_PERDIDO)]
    total_abierto = sum(float(o.valor or 0) for o in abiertas)
    total_ponderado = sum(o.valor_ponderado for o in abiertas)
    ganadas = [op for op in rows if op.etapa == EtapaOportunidad.CIERRE_GANADO]
    perdidas = [op for op in rows if op.etapa == EtapaOportunidad.CIERRE_PERDIDO]

    return jsonify({
        "etapas": [grouped[e.value] for e in EtapaOportunidad],
        "summary": {
            "abiertas_count": len(abiertas),
            "abiertas_valor": total_abierto,
            "abiertas_valor_ponderado": round(total_ponderado, 2),
            "ganadas_count": len(ganadas),
            "ganadas_valor": sum(float(o.valor or 0) for o in ganadas),
            "perdidas_count": len(perdidas),
            "perdidas_valor": sum(float(o.valor or 0) for o in perdidas),
        },
    })


# ── Create ────────────────────────────────────────────────────────


@oportunidades_bp.route("/", methods=["POST"])
def create_oportunidad():
    """Crea una oportunidad. Si no se pasa propietario_id, asigna al
    usuario en sesión."""
    data = request.get_json() or {}
    if not data.get("nombre"):
        return jsonify({"error": "nombre es requerido"}), 400
    etapa = _parse_etapa(data.get("etapa")) or EtapaOportunidad.CALIFICACION

    op = Oportunidad(
        nombre=data["nombre"],
        empresa=data.get("empresa"),
        contacto_nombre=data.get("contacto_nombre"),
        contacto_telefono=data.get("contacto_telefono"),
        contacto_email=data.get("contacto_email"),
        valor=_to_decimal(data.get("valor")) or Decimal("0"),
        moneda=data.get("moneda") or "MXN",
        fecha_cierre_esperada=_parse_date(data.get("fecha_cierre_esperada")),
        etapa=etapa,
        propietario_id=data.get("propietario_id") or _current_user_id(),
        marca_interes=data.get("marca_interes"),
        estado_cliente=data.get("estado_cliente"),
        num_sucursales=data.get("num_sucursales"),
        monthly_amount=_to_decimal(data.get("monthly_amount")),
        sale_type=data.get("sale_type"),
        notas=data.get("notas"),
        lead_id=data.get("lead_id"),
        zoho_deal_id=data.get("zoho_deal_id"),
        account_id=data.get("account_id"),
        contact_id=data.get("contact_id"),
    )
    if "probabilidad" in data:
        try:
            op.probabilidad = max(0, min(100, int(data["probabilidad"])))
        except (ValueError, TypeError):
            pass
    db.session.add(op)
    db.session.commit()
    return jsonify(op.to_dict()), 201


# ── Update ────────────────────────────────────────────────────────


@oportunidades_bp.route("/<uuid:opp_id>", methods=["PATCH"])
def update_oportunidad(opp_id):
    op = db.session.get(Oportunidad, opp_id)
    if not op:
        return jsonify({"error": "Oportunidad no encontrada"}), 404
    data = request.get_json() or {}

    for fld in ("nombre", "empresa", "contacto_nombre", "contacto_telefono",
                "contacto_email", "moneda", "marca_interes", "estado_cliente",
                "num_sucursales", "sale_type", "notas", "motivo_perdida",
                "propietario_id", "account_id", "contact_id"):
        if fld in data:
            setattr(op, fld, data[fld])

    if "valor" in data:
        op.valor = _to_decimal(data["valor"]) or Decimal("0")
    if "monthly_amount" in data:
        op.monthly_amount = _to_decimal(data["monthly_amount"])
    if "fecha_cierre_esperada" in data:
        op.fecha_cierre_esperada = _parse_date(data["fecha_cierre_esperada"])
    if "etapa" in data:
        new_etapa = _parse_etapa(data["etapa"])
        if new_etapa:
            op.etapa = new_etapa
    if "probabilidad" in data:
        try:
            op.probabilidad = max(0, min(100, int(data["probabilidad"])))
        except (ValueError, TypeError):
            pass

    db.session.commit()
    return jsonify(op.to_dict())


@oportunidades_bp.route("/<uuid:opp_id>/mover", methods=["PATCH"])
def mover_oportunidad(opp_id):
    """Atajo para drag&drop del Kanban: solo cambia etapa + probabilidad."""
    op = db.session.get(Oportunidad, opp_id)
    if not op:
        return jsonify({"error": "Oportunidad no encontrada"}), 404
    data = request.get_json() or {}
    nueva = _parse_etapa(data.get("etapa"))
    if not nueva:
        return jsonify({"error": "Etapa inválida"}), 400
    op.etapa = nueva
    db.session.commit()
    return jsonify(op.to_dict())


@oportunidades_bp.route("/<uuid:opp_id>", methods=["DELETE"])
def delete_oportunidad(opp_id):
    op = db.session.get(Oportunidad, opp_id)
    if not op:
        return jsonify({"error": "Oportunidad no encontrada"}), 404
    db.session.delete(op)
    db.session.commit()
    return jsonify({"ok": True})


# ── Convertir Lead → Oportunidad ──────────────────────────────────


@oportunidades_bp.route("/from-lead/<uuid:lead_id>", methods=["POST"])
def from_lead(lead_id):
    """Crea una Oportunidad pre-rellenada con datos del Lead. El body puede
    sobrescribir cualquier campo. Mantiene FK lead_id → traza el origen."""
    lead = db.session.get(Lead, lead_id)
    if not lead:
        return jsonify({"error": "Lead no encontrado"}), 404
    data = request.get_json() or {}

    # Defaults desde el Lead
    valor_inicial = (
        _to_decimal(data.get("valor")) or
        (lead.valor_estimado and Decimal(str(lead.valor_estimado))) or
        Decimal("0")
    )
    monthly = (
        _to_decimal(data.get("monthly_amount")) or
        (lead.precio_unitario and Decimal(str(lead.precio_unitario))) or
        None
    )
    op = Oportunidad(
        nombre=data.get("nombre") or f"{lead.empresa_nombre or lead.nombre} — {lead.marca_interes or ''}".strip(" —"),
        empresa=data.get("empresa") or lead.empresa_nombre,
        contacto_nombre=data.get("contacto_nombre") or lead.nombre,
        contacto_telefono=data.get("contacto_telefono") or lead.telefono,
        contacto_email=data.get("contacto_email"),
        valor=valor_inicial,
        moneda=data.get("moneda") or "MXN",
        fecha_cierre_esperada=_parse_date(data.get("fecha_cierre_esperada")),
        etapa=_parse_etapa(data.get("etapa")) or EtapaOportunidad.CALIFICACION,
        propietario_id=(data.get("propietario_id")
                        or str(lead.usuario_asignado_id) if lead.usuario_asignado_id else None
                        or _current_user_id()),
        marca_interes=data.get("marca_interes") or lead.marca_interes,
        estado_cliente=data.get("estado_cliente") or lead.estado_cliente,
        num_sucursales=data.get("num_sucursales") or lead.num_sucursales,
        monthly_amount=monthly,
        sale_type=data.get("sale_type"),
        notas=data.get("notas") or lead.notas,
        lead_id=lead.id,
    )
    db.session.add(op)
    # Marca lead como convertido subiéndolo de etapa si todavía no llegó
    if lead.etapa_pipeline and lead.etapa_pipeline.value not in ("Cerrado Ganado", "Cerrado Perdido"):
        if lead.etapa_pipeline not in (EtapaPipeline.NEGOCIACION, EtapaPipeline.COTIZACION,
                                         EtapaPipeline.DEMO):
            lead.etapa_pipeline = EtapaPipeline.COTIZACION
    db.session.commit()
    return jsonify(op.to_dict()), 201


# ── Stats ─────────────────────────────────────────────────────────


@oportunidades_bp.route("/stats", methods=["GET"])
def stats():
    base = Oportunidad.query
    rows = (
        base.with_entities(Oportunidad.etapa, func.count(),
                           func.coalesce(func.sum(Oportunidad.valor), 0))
        .group_by(Oportunidad.etapa).all()
    )
    by_etapa = []
    total_count = 0
    total_valor = 0.0
    for et, c, v in rows:
        by_etapa.append({
            "etapa": et.value if et else "—",
            "count": int(c), "valor": float(v or 0),
            "probabilidad": PROBABILIDAD_OPORTUNIDAD.get(et, 0),
        })
        total_count += int(c)
        total_valor += float(v or 0)

    by_marca = (
        base.with_entities(Oportunidad.marca_interes, func.count(),
                           func.coalesce(func.sum(Oportunidad.valor), 0))
        .filter(Oportunidad.marca_interes.isnot(None))
        .group_by(Oportunidad.marca_interes).all()
    )
    return jsonify({
        "total_count": total_count, "total_valor": total_valor,
        "by_etapa": by_etapa,
        "by_marca": [{"marca": m, "count": int(c), "valor": float(v or 0)}
                      for m, c, v in by_marca],
    })

"""
CRUD admin para el registry de campañas Meta Ads.

Rutas bajo /api/meta-campaigns. Solo lectura sin auth especial (cualquier
sesión); writes requieren rol Super Admin.
"""
from datetime import datetime, timezone
from flask import Blueprint, request, jsonify, session
from sqlalchemy import func

from extensions import db
from models import MetaCampaign, Lead
import meta_campaign_registry

meta_campaigns_bp = Blueprint("meta_campaigns", __name__)


def _is_admin():
    return (session.get("user_rol", "") or "").lower().replace(" ", "_") == "super_admin"


# ── Listar ──────────────────────────────────────────────────────────


@meta_campaigns_bp.route("", methods=["GET"])
@meta_campaigns_bp.route("/", methods=["GET"])
def list_campaigns():
    """Lista campañas + conteo de leads matcheados (todos los Lead con ese campaign_id)."""
    rows = MetaCampaign.query.order_by(MetaCampaign.activa.desc(), MetaCampaign.nombre.asc()).all()
    # Leads count por campaign_id (un único query agrupada para evitar N+1)
    leads_rows = (
        db.session.query(Lead.meta_campaign, func.count(Lead.id))
        .filter(Lead.meta_campaign.isnot(None))
        .group_by(Lead.meta_campaign)
        .all()
    )
    leads_map = {cid: cnt for cid, cnt in leads_rows}
    out = []
    for r in rows:
        d = r.to_dict()
        d["leads_count"] = int(leads_map.get(r.campaign_id, 0))
        out.append(d)
    return jsonify({
        "campaigns": out,
        "presets": meta_campaign_registry.ZONA_PRESETS,
    })


# ── Crear ───────────────────────────────────────────────────────────


@meta_campaigns_bp.route("", methods=["POST"])
@meta_campaigns_bp.route("/", methods=["POST"])
def create_campaign():
    if not _is_admin():
        return jsonify({"error": "Solo Super Admin puede modificar el registry"}), 403
    data = request.get_json() or {}
    cid = (data.get("campaign_id") or "").strip()
    nombre = (data.get("nombre") or "").strip()
    marca = (data.get("marca") or "").strip()
    unidad = (data.get("unidad") or "").strip()
    if not cid or not nombre or not marca or not unidad:
        return jsonify({"error": "campaign_id, nombre, marca y unidad son requeridos"}), 400
    if MetaCampaign.query.get(cid):
        return jsonify({"error": f"Ya existe una campaña con campaign_id={cid}"}), 409

    row = MetaCampaign(
        campaign_id=cid,
        nombre=nombre,
        marca=marca,
        unidad=unidad,
        estado_default=(data.get("estado_default") or None) or None,
        zonas=list(data.get("zonas") or []),
        activa=bool(data.get("activa", True)),
    )
    try:
        db.session.add(row)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 500

    meta_campaign_registry.invalidate()
    return jsonify(row.to_dict()), 201


# ── Editar ──────────────────────────────────────────────────────────


@meta_campaigns_bp.route("/<campaign_id>", methods=["PUT"])
def update_campaign(campaign_id):
    if not _is_admin():
        return jsonify({"error": "Solo Super Admin puede modificar el registry"}), 403
    row = MetaCampaign.query.get(campaign_id)
    if not row:
        return jsonify({"error": "Campaña no encontrada"}), 404
    data = request.get_json() or {}

    if "nombre" in data and data["nombre"]:         row.nombre = data["nombre"].strip()
    if "marca" in data and data["marca"]:           row.marca = data["marca"].strip()
    if "unidad" in data and data["unidad"]:         row.unidad = data["unidad"].strip()
    if "estado_default" in data:                    row.estado_default = (data["estado_default"] or None) or None
    if "zonas" in data and isinstance(data["zonas"], list): row.zonas = list(data["zonas"])
    if "activa" in data:                            row.activa = bool(data["activa"])

    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 500

    meta_campaign_registry.invalidate()
    return jsonify(row.to_dict())


# ── Borrar ──────────────────────────────────────────────────────────


@meta_campaigns_bp.route("/<campaign_id>", methods=["DELETE"])
def delete_campaign(campaign_id):
    if not _is_admin():
        return jsonify({"error": "Solo Super Admin puede modificar el registry"}), 403
    row = MetaCampaign.query.get(campaign_id)
    if not row:
        return jsonify({"error": "Campaña no encontrada"}), 404
    try:
        db.session.delete(row)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 500

    meta_campaign_registry.invalidate()
    return jsonify({"ok": True, "campaign_id": campaign_id})


# ── Detección de campañas no registradas (utilidad) ────────────────


@meta_campaigns_bp.route("/unregistered", methods=["GET"])
def list_unregistered():
    """Lista campaign_ids que aparecen en leads pero NO están en el registry.
    Útil para que el admin sepa qué campañas nuevas necesitan darse de alta."""
    registered = {r.campaign_id for r in MetaCampaign.query.all()}
    rows = (
        db.session.query(
            Lead.meta_campaign,
            func.count(Lead.id),
            func.max(Lead.fecha_creacion),
        )
        .filter(Lead.meta_campaign.isnot(None))
        .group_by(Lead.meta_campaign)
        .all()
    )
    out = []
    for cid, cnt, last in rows:
        if cid in registered:
            continue
        out.append({
            "campaign_id": cid,
            "leads_count": int(cnt),
            "ultimo_lead": last.isoformat() if last else None,
        })
    out.sort(key=lambda x: x["leads_count"], reverse=True)
    return jsonify({"unregistered": out})

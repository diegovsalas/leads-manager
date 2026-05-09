# blueprints/scip.py
"""
SCIP — Sistema de Control de Inversión Publicitaria.

SCAFFOLD ONLY — port parcial. Lo que SÍ está aquí:
- CRUD sobre scip_director_recommendations (decisiones del director sobre
  campañas Meta/Google Ads).

Lo que NO está y queda como TODO documentado (ver _legacy/vendedores-cloud/scip/):
- Meta Marketing API integration (~845+647 líneas de service+routes JS).
  Requiere SDK facebook-business + env vars META_ACCESS_TOKEN, META_APP_ID,
  META_API_VERSION, META_ACCOUNT_B2C/B2B/WELDU.
- Google Ads API integration (~277+178 líneas). Requiere SDK google-ads +
  env vars GOOGLE_ADS_CLIENT_ID/SECRET/REFRESH_TOKEN/DEVELOPER_TOKEN/
  LOGIN_CUSTOMER_ID.
- Director routes con fetch real de campañas (587 líneas legacy).
- MarketInsito chat (Anthropic-powered ad analyst).

Para implementar el lado Meta/Google se necesita:
  pip install facebook-business google-ads
y un round dedicado por SDK.
"""
from datetime import datetime, timezone
from flask import Blueprint, request, jsonify, session
from sqlalchemy import func

from extensions import db
from models import ScipDirectorRecommendation

scip_bp = Blueprint("scip", __name__)


def _parse_dt(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError, AttributeError):
        return None


# ── Director recommendations CRUD ──────────────────────────────────


@scip_bp.route("/director/recommendations", methods=["GET"])
def list_recommendations():
    """Filtros: ?status= ?platform= ?unit= ?director_user_id="""
    q = ScipDirectorRecommendation.query
    for arg, col in [
        ("status", ScipDirectorRecommendation.status),
        ("platform", ScipDirectorRecommendation.campaign_platform),
        ("unit", ScipDirectorRecommendation.campaign_unit),
        ("director_user_id", ScipDirectorRecommendation.director_user_id),
    ]:
        v = request.args.get(arg)
        if v:
            q = q.filter(col == v)
    rows = q.order_by(ScipDirectorRecommendation.created_at.desc()).all()
    return jsonify([r.to_dict() for r in rows])


@scip_bp.route("/director/recommendations/<int:rec_id>", methods=["GET"])
def get_recommendation(rec_id):
    rec = db.session.get(ScipDirectorRecommendation, rec_id)
    if not rec:
        return jsonify({"error": "Recomendación no encontrada"}), 404
    return jsonify(rec.to_dict())


@scip_bp.route("/director/recommendations", methods=["POST"])
def create_recommendation():
    d = request.get_json() or {}
    required = ("campaign_id", "campaign_name", "decided_action")
    missing = [f for f in required if not d.get(f)]
    if missing:
        return jsonify({"error": f"campos requeridos: {missing}"}), 400

    rec = ScipDirectorRecommendation(
        campaign_id=d["campaign_id"], campaign_name=d["campaign_name"],
        campaign_platform=d.get("campaign_platform"),
        campaign_unit=d.get("campaign_unit"),
        director_user_id=session.get("user_id") or d.get("director_user_id"),
        director_name=session.get("user_nombre") or d.get("director_name") or "Director",
        decided_action=d["decided_action"],
        scale_to_campaign_id=d.get("scale_to_campaign_id"),
        scale_to_campaign_name=d.get("scale_to_campaign_name"),
        rationale=d.get("rationale"),
        data_snapshot_json=d.get("data_snapshot"),
        options_snapshot_json=d.get("options_snapshot"),
        ad_id=d.get("ad_id"), ad_name=d.get("ad_name"),
        seller_user_id=d.get("seller_user_id"),
        seller_name=d.get("seller_name"),
        scale_to_seller_id=d.get("scale_to_seller_id"),
        scale_to_seller_name=d.get("scale_to_seller_name"),
    )
    db.session.add(rec)
    db.session.commit()
    return jsonify(rec.to_dict()), 201


@scip_bp.route("/director/recommendations/<int:rec_id>", methods=["PATCH"])
def update_recommendation(rec_id):
    rec = db.session.get(ScipDirectorRecommendation, rec_id)
    if not rec:
        return jsonify({"error": "Recomendación no encontrada"}), 404
    d = request.get_json() or {}

    for fld in ("status", "marketing_notes", "rationale",
                "scale_to_campaign_id", "scale_to_campaign_name"):
        if fld in d:
            setattr(rec, fld, d[fld])

    if d.get("status") == "executed" and not rec.executed_at:
        rec.executed_at = datetime.now(timezone.utc)
        rec.executed_by_user_id = session.get("user_id") or rec.executed_by_user_id
        rec.executed_by_name = session.get("user_nombre") or rec.executed_by_name

    db.session.commit()
    return jsonify(rec.to_dict())


@scip_bp.route("/director/recommendations/<int:rec_id>", methods=["DELETE"])
def delete_recommendation(rec_id):
    rec = db.session.get(ScipDirectorRecommendation, rec_id)
    if not rec:
        return jsonify({"error": "Recomendación no encontrada"}), 404
    db.session.delete(rec)
    db.session.commit()
    return jsonify({"ok": True})


@scip_bp.route("/director/stats", methods=["GET"])
def director_stats():
    """Resumen: total / pending / executed por director y plataforma."""
    rows = (
        db.session.query(
            ScipDirectorRecommendation.status,
            func.count(),
        ).group_by(ScipDirectorRecommendation.status).all()
    )
    by_platform = (
        db.session.query(
            ScipDirectorRecommendation.campaign_platform,
            func.count(),
        ).group_by(ScipDirectorRecommendation.campaign_platform).all()
    )
    return jsonify({
        "by_status": [{"status": s, "count": c} for s, c in rows],
        "by_platform": [{"platform": p or "unknown", "count": c} for p, c in by_platform],
        "total": sum(c for _, c in rows),
    })


# ── Stubs para integraciones Meta/Google Ads (TODO) ────────────────


@scip_bp.route("/meta/campaigns", methods=["GET"])
def meta_campaigns_stub():
    return jsonify({
        "error": "meta_ads_not_configured",
        "message": "SCIP Meta integration pendiente. Ver scip/meta-ads.* en _legacy.",
        "todo": [
            "pip install facebook-business",
            "Configurar META_ACCESS_TOKEN, META_APP_ID, META_API_VERSION",
            "Configurar META_ACCOUNT_B2C, META_ACCOUNT_B2B, META_ACCOUNT_WELDU",
            "Portar scip/meta-ads.service.js (~845 líneas) a Python",
        ],
    }), 501


@scip_bp.route("/google/campaigns", methods=["GET"])
def google_campaigns_stub():
    return jsonify({
        "error": "google_ads_not_configured",
        "message": "SCIP Google Ads integration pendiente. Ver scip/google-ads.* en _legacy.",
        "todo": [
            "pip install google-ads",
            "Configurar GOOGLE_ADS_CLIENT_ID/SECRET/REFRESH_TOKEN",
            "Configurar GOOGLE_ADS_DEVELOPER_TOKEN, GOOGLE_ADS_LOGIN_CUSTOMER_ID",
            "Portar scip/google-ads.service.js (~277 líneas) a Python",
        ],
    }), 501

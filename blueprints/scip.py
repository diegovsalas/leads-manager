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
import scip_meta
import scip_google

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


# ── Meta Ads endpoints (live, port de meta-ads.service.js) ─────────


def _safe(fn, *args, **kwargs):
    """Wrapper: si la operación tira, devuelve {error}."""
    try:
        return jsonify(fn(*args, **kwargs))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@scip_bp.route("/meta/health", methods=["GET"])
def meta_health():
    return jsonify(scip_meta.health())


@scip_bp.route("/meta/campaigns", methods=["GET"])
def meta_campaigns():
    """?account=aromatex_b2c|aromatex_b2b|weldu (default aromatex_b2c)"""
    account = request.args.get("account", "aromatex_b2c")
    return _safe(scip_meta.get_campaigns, account)


@scip_bp.route("/meta/campaigns/<campaign_id>/adsets", methods=["GET"])
def meta_adsets(campaign_id):
    return _safe(scip_meta.get_adsets_by_campaign, campaign_id)


@scip_bp.route("/meta/adsets/<adset_id>/ads", methods=["GET"])
def meta_ads(adset_id):
    return _safe(scip_meta.get_ads_by_adset, adset_id)


@scip_bp.route("/meta/metrics/campaign/<campaign_id>", methods=["GET"])
def meta_campaign_metrics(campaign_id):
    return _safe(scip_meta.get_campaign_metrics, campaign_id)


@scip_bp.route("/meta/daily-metrics", methods=["GET"])
def meta_daily():
    """?account=&since=YYYY-MM-DD&until=YYYY-MM-DD"""
    account = request.args.get("account", "aromatex_b2c")
    since = request.args.get("since")
    until = request.args.get("until")
    if not since or not until:
        return jsonify({"error": "since y until requeridos (YYYY-MM-DD)"}), 400
    return _safe(scip_meta.get_account_daily_insights, account, since, until)


@scip_bp.route("/meta/full-sync", methods=["GET"])
def meta_full_sync():
    """Pull pesado: campaigns + adsets + ads. Cached 5min."""
    account = request.args.get("account", "aromatex_b2c")
    return _safe(scip_meta.get_full_sync, account)


@scip_bp.route("/meta/marketinsito-report", methods=["GET"])
def meta_marketinsito_report():
    """Reporte ejecutivo: summary + campaigns + alerts + recommendations."""
    account = request.args.get("account", "aromatex_b2c")
    return _safe(scip_meta.get_marketinsito_report, account)


@scip_bp.route("/meta/creative-performance", methods=["GET"])
def meta_creative_perf():
    """Top 50 creativos por spend. Para SCIP director: qué ad escalar."""
    account = request.args.get("account", "aromatex_b2c")
    return _safe(scip_meta.get_creative_performance, account)


@scip_bp.route("/meta/cache/flush", methods=["POST"])
def meta_flush():
    return jsonify(scip_meta.flush_cache())


# ── Google Ads endpoints (live, port de google-ads.service.js) ─────


@scip_bp.route("/google/health", methods=["GET"])
def google_health():
    return jsonify(scip_google.health())


@scip_bp.route("/google/customers", methods=["GET"])
def google_customers():
    """Lista los customer IDs accesibles con el refresh token actual."""
    return jsonify(scip_google.list_accessible_customers())


@scip_bp.route("/google/campaigns", methods=["GET"])
def google_campaigns():
    """?customer_id= (opcional, default LOGIN_CUSTOMER_ID)"""
    cid = request.args.get("customer_id")
    return jsonify(scip_google.get_campaigns(cid))


@scip_bp.route("/google/campaigns/<campaign_id>", methods=["GET"])
def google_campaign(campaign_id):
    cid = request.args.get("customer_id")
    return jsonify(scip_google.get_campaign(cid, campaign_id))


@scip_bp.route("/google/campaigns/<campaign_id>/adgroups", methods=["GET"])
def google_adgroups(campaign_id):
    cid = request.args.get("customer_id")
    return jsonify(scip_google.get_ad_groups(cid, campaign_id))


@scip_bp.route("/google/metrics", methods=["GET"])
def google_metrics():
    """?customer_id=&days=30"""
    cid = request.args.get("customer_id")
    try:
        days = int(request.args.get("days", "30"))
    except ValueError:
        days = 30
    return jsonify(scip_google.get_metrics(cid, days))


@scip_bp.route("/google/daily-metrics", methods=["GET"])
def google_daily():
    cid = request.args.get("customer_id")
    try:
        days = int(request.args.get("days", "30"))
    except ValueError:
        days = 30
    return jsonify(scip_google.get_daily_metrics(cid, days))


@scip_bp.route("/google/cache/flush", methods=["POST"])
def google_flush():
    return jsonify(scip_google.flush_cache())

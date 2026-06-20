"""
API admin para monitoreo de correos salientes de vendedores.

Rutas bajo /api/sales-emails. Solo lectura. Requiere rol Super Admin.
"""
from datetime import datetime, timedelta, timezone
from flask import Blueprint, request, jsonify, session
from sqlalchemy import func, desc

from extensions import db
from models import SalesEmail, Usuario
import gmail_monitor

sales_emails_bp = Blueprint("sales_emails", __name__)


def _is_admin():
    return (session.get("user_rol", "") or "").lower().replace(" ", "_") == "super_admin"


def _require_admin():
    if not _is_admin():
        return jsonify({"error": "Solo Super Admin puede ver el monitoreo de correos"}), 403
    return None


# ── KPIs por vendedor ───────────────────────────────────────────────


@sales_emails_bp.route("/stats", methods=["GET"])
def stats():
    err = _require_admin()
    if err: return err

    now = datetime.now(timezone.utc)
    hoy_inicio = now.replace(hour=0, minute=0, second=0, microsecond=0)
    semana_inicio = hoy_inicio - timedelta(days=7)
    mes_inicio = hoy_inicio - timedelta(days=30)

    # Vendedores con gmail_address configurado
    vendedores = (
        Usuario.query
        .filter(Usuario.gmail_address.isnot(None), Usuario.gmail_address != "")
        .order_by(Usuario.nombre.asc()).all()
    )

    # Counts agrupados por vendedor (3 ventanas) en queries separadas
    def count_since(since):
        rows = (
            db.session.query(SalesEmail.vendedor_id, func.count(SalesEmail.id))
            .filter(SalesEmail.sent_at >= since)
            .group_by(SalesEmail.vendedor_id).all()
        )
        return {str(r[0]): int(r[1]) for r in rows}

    counts_hoy = count_since(hoy_inicio)
    counts_7d = count_since(semana_inicio)
    counts_30d = count_since(mes_inicio)

    # Último envío por vendedor
    last_rows = (
        db.session.query(SalesEmail.vendedor_id, func.max(SalesEmail.sent_at))
        .group_by(SalesEmail.vendedor_id).all()
    )
    last_map = {str(r[0]): r[1] for r in last_rows}

    data = []
    for v in vendedores:
        vid = str(v.id)
        ultimo = last_map.get(vid)
        data.append({
            "vendedor_id":    vid,
            "vendedor":       v.nombre,
            "gmail_address":  v.gmail_address,
            "marcas":         list(v.especialidad_marca or []),
            "hoy":            counts_hoy.get(vid, 0),
            "7d":             counts_7d.get(vid, 0),
            "30d":            counts_30d.get(vid, 0),
            "ultimo_envio":   ultimo.isoformat() if ultimo else None,
        })

    # Totales del portfolio
    totales = {
        "hoy":  sum(d["hoy"] for d in data),
        "7d":   sum(d["7d"]  for d in data),
        "30d":  sum(d["30d"] for d in data),
    }

    return jsonify({
        "vendedores":    data,
        "totales":       totales,
        "configurado":   gmail_monitor.is_configured(),
        "internal_dom":  gmail_monitor.INTERNAL_DOMAIN,
    })


# ── Listado de correos por vendedor ─────────────────────────────────


@sales_emails_bp.route("/", methods=["GET"])
def listar():
    err = _require_admin()
    if err: return err

    vendedor_id = request.args.get("vendedor_id")
    days = int(request.args.get("days") or 7)
    limit = min(int(request.args.get("limit") or 100), 500)

    since = datetime.now(timezone.utc) - timedelta(days=days)
    q = SalesEmail.query.filter(SalesEmail.sent_at >= since)
    if vendedor_id:
        q = q.filter(SalesEmail.vendedor_id == vendedor_id)
    rows = q.order_by(desc(SalesEmail.sent_at)).limit(limit).all()
    return jsonify({"emails": [r.to_dict() for r in rows], "count": len(rows)})


# ── Disparar poll manual (debug / forzar refresh) ──────────────────


@sales_emails_bp.route("/poll", methods=["POST"])
def trigger_poll():
    err = _require_admin()
    if err: return err
    lookback = int(request.args.get("lookback_min") or gmail_monitor.LOOKBACK_MIN)
    try:
        return jsonify(gmail_monitor.poll_all(lookback_min=lookback))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@sales_emails_bp.route("/test", methods=["GET"])
def test_auth():
    """Prueba autenticación con un email específico. Devuelve OK + perfil del
    user impersonado, o el error exacto de Google.
    Uso: GET /api/sales-emails/test?email=katyagomez@grupoavantex.com
    """
    err = _require_admin()
    if err: return err

    email = (request.args.get("email") or "").strip()
    if not email:
        return jsonify({"error": "Falta ?email=..."}), 400
    if not gmail_monitor.is_configured():
        return jsonify({"error": "GMAIL_SERVICE_ACCOUNT_JSON no configurado en Render"}), 500

    try:
        svc = gmail_monitor._build_service(email)
        profile = svc.users().getProfile(userId="me").execute()
        return jsonify({
            "ok": True,
            "email_probado": email,
            "profile": {
                "emailAddress":     profile.get("emailAddress"),
                "messagesTotal":    profile.get("messagesTotal"),
                "threadsTotal":     profile.get("threadsTotal"),
                "historyId":        profile.get("historyId"),
            }
        })
    except Exception as e:
        # Aplanamos el error de Google para que sea legible en el UI
        from google.auth.exceptions import RefreshError
        from googleapiclient.errors import HttpError
        if isinstance(e, RefreshError):
            return jsonify({
                "ok": False,
                "tipo_error": "auth/impersonation",
                "mensaje": str(e),
                "diagnostico": "Casi seguro: delegación no autorizada en admin.google.com, o el email no existe en Workspace, o domain-wide delegation no marcada en el service account.",
            }), 500
        if isinstance(e, HttpError):
            return jsonify({
                "ok": False,
                "tipo_error": "gmail_api",
                "status": e.resp.status,
                "mensaje": str(e),
            }), 500
        return jsonify({"ok": False, "tipo_error": "desconocido", "mensaje": str(e)}), 500

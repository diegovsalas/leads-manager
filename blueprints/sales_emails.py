"""
API admin para monitoreo de correos salientes de vendedores.

Rutas bajo /api/sales-emails. Solo lectura. Requiere rol Super Admin.
"""
import base64
from datetime import datetime, timedelta, timezone
from flask import Blueprint, request, jsonify, session, Response
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


@sales_emails_bp.route("/<uuid:email_id>", methods=["GET"])
def get_email(email_id):
    """Devuelve un correo con body_text + body_html completos."""
    err = _require_admin()
    if err: return err
    email = SalesEmail.query.get(str(email_id))
    if not email:
        return jsonify({"error": "Correo no encontrado"}), 404
    return jsonify(email.to_dict(include_body=True))


# ── Disparar poll manual (debug / forzar refresh) ──────────────────


@sales_emails_bp.route("/poll", methods=["POST", "GET"])
def trigger_poll():
    """Dispara poll manual. Acepta GET para ser pegable desde el navegador
    (sesión activa).
      ?lookback_min=N  override de ventana. Recomendados: 1440=24h, 10080=7d, 43200=30d
      ?force=1         re-fetch incluso si el correo ya tiene body (refresca
                       attachment_id u otros campos nuevos del schema)
    """
    err = _require_admin()
    if err: return err
    lookback = int(request.args.get("lookback_min") or gmail_monitor.LOOKBACK_MIN)
    force = (request.args.get("force") or "").strip() in ("1", "true", "yes")
    try:
        return jsonify(gmail_monitor.poll_all(lookback_min=lookback, force_refresh=force))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@sales_emails_bp.route("/refresh-all", methods=["GET", "POST"])
def refresh_all():
    """Itera TODOS los SalesEmail en BD y los re-fetcha individualmente desde
    Gmail. Útil para refrescar attachment_id u otros campos del schema sin
    depender del filtro Gmail/lookback.
    """
    err = _require_admin()
    if err: return err
    if not gmail_monitor.is_configured():
        return jsonify({"error": "GMAIL_SERVICE_ACCOUNT_JSON no configurado"}), 500

    # Si ?missing_att_id_only=1, solo procesa los que tienen attachment sin id
    only_missing = (request.args.get("missing_att_id_only") or "").strip() in ("1", "true", "yes")

    stats = {"total": 0, "refreshed": 0, "errors": 0, "skipped": 0}

    # Cache de servicio por vendedora (evita rebuilding por cada correo)
    svc_cache = {}

    rows = SalesEmail.query.order_by(SalesEmail.sent_at.desc()).all()
    stats["total"] = len(rows)

    for row in rows:
        if only_missing:
            atts = row.attachments or []
            has_unfilled = any(a.get("filename") and not a.get("attachment_id") for a in atts)
            if not has_unfilled:
                stats["skipped"] += 1
                continue

        vendedor = row.vendedor
        if not vendedor or not vendedor.gmail_address:
            stats["errors"] += 1
            continue
        addr = vendedor.gmail_address
        try:
            if addr not in svc_cache:
                svc_cache[addr] = gmail_monitor._build_service(addr)
            svc = svc_cache[addr]
            msg = svc.users().messages().get(userId="me", id=row.gmail_message_id, format="full").execute()
        except Exception as e:
            stats["errors"] += 1
            continue

        parsed = gmail_monitor._parse_gmail_message(msg)
        if not parsed:
            stats["skipped"] += 1
            continue

        try:
            for k, v in parsed.items():
                if k != "gmail_message_id":
                    setattr(row, k, v)
            db.session.flush()
            stats["refreshed"] += 1
        except Exception:
            db.session.rollback()
            stats["errors"] += 1

    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e), **stats}), 500

    return jsonify(stats)


@sales_emails_bp.route("/<uuid:email_id>/attachment/<int:idx>", methods=["GET"])
def download_attachment(email_id, idx):
    """Descarga un adjunto on-demand desde Gmail. NO se guarda en BD.
    URL: /api/sales-emails/<id>/attachment/<n> donde n es el índice del
    adjunto en email.attachments[].
    """
    err = _require_admin()
    if err: return err

    from flask import current_app
    import time as _time

    email = SalesEmail.query.get(str(email_id))
    if not email:
        return jsonify({"error": "Correo no encontrado"}), 404
    atts = email.attachments or []
    if idx < 0 or idx >= len(atts):
        return jsonify({"error": "Adjunto no encontrado"}), 404
    att = atts[idx]
    att_id = att.get("attachment_id")
    if not att_id:
        return jsonify({
            "error": "attachment_id no almacenado para este correo. Ejecuta /api/sales-emails/poll?force=1 para refrescar."
        }), 400

    vendedor = email.vendedor
    if not vendedor or not vendedor.gmail_address:
        return jsonify({"error": "Vendedora sin gmail_address"}), 400

    filename = att.get("filename") or "adjunto"
    size_kb = (att.get("size") or 0) // 1024
    current_app.logger.info(
        f"[attachment] download iniciada: vendedor={vendedor.gmail_address} "
        f"email_id={email_id} filename={filename} size_kb={size_kb}"
    )

    t0 = _time.time()
    try:
        svc = gmail_monitor._build_service(vendedor.gmail_address)
        result = svc.users().messages().attachments().get(
            userId="me", messageId=email.gmail_message_id, id=att_id,
        ).execute()
    except Exception as e:
        current_app.logger.exception(f"[attachment] Gmail API falló")
        return jsonify({"error": f"Error consultando Gmail: {type(e).__name__}: {str(e)[:200]}"}), 502

    data_b64 = result.get("data") or ""
    if not data_b64:
        # Gmail puede devolver attachmentId vacío si el archivo es muy grande
        # y requiere descarga paginada (>= 25MB típicamente)
        current_app.logger.warning(f"[attachment] sin data: file probablemente >25MB")
        return jsonify({"error": "Archivo muy grande (>25MB). Gmail no lo devolvió inline. Abre el correo directo en Gmail con el link."}), 413

    try:
        raw = base64.urlsafe_b64decode(data_b64)
    except Exception as e:
        current_app.logger.exception(f"[attachment] base64 decode falló")
        return jsonify({"error": f"Archivo corrupto en Gmail: {e}"}), 500

    elapsed_ms = int((_time.time() - t0) * 1000)
    current_app.logger.info(f"[attachment] OK: {len(raw)} bytes en {elapsed_ms}ms ({filename})")

    mime = att.get("mime_type") or "application/octet-stream"
    # Sanea filename para Content-Disposition (sin comillas, sin newlines)
    safe_name = filename.replace('"', "").replace("\n", "").replace("\r", "")
    return Response(
        raw,
        mimetype=mime,
        headers={
            "Content-Disposition": f'attachment; filename="{safe_name}"',
            "Content-Length": str(len(raw)),
            "Cache-Control": "private, max-age=300",  # cliente cachea 5min
        },
    )


@sales_emails_bp.route("/diagnose", methods=["GET"])
def diagnose_vendor():
    """Diagnóstico de correos enviados de un vendedor. Cuenta mensajes con
    diferentes filtros para identificar si el problema es:
      - No manda correos en general
      - Solo manda a internos (filtrados por el monitoreo)
      - El filtro de exclusión @grupoavantex.com está mal
    Uso: GET /api/sales-emails/diagnose?email=angelicauribe@grupoavantex.com
    """
    err = _require_admin()
    if err: return err
    email = (request.args.get("email") or "").strip()
    if not email:
        return jsonify({"error": "Falta ?email=..."}), 400
    if not gmail_monitor.is_configured():
        return jsonify({"error": "GMAIL_SERVICE_ACCOUNT_JSON no configurado"}), 500

    try:
        svc = gmail_monitor._build_service(email)
    except Exception as e:
        return jsonify({"error": f"Auth falló: {e}"}), 500

    from datetime import datetime, timedelta, timezone
    def _count(q):
        try:
            r = svc.users().messages().list(userId="me", q=q, maxResults=1).execute()
            return int(r.get("resultSizeEstimate") or 0)
        except Exception as e:
            return f"error: {e}"

    def _last_sent(q):
        try:
            r = svc.users().messages().list(userId="me", q=q, maxResults=1).execute()
            msgs = r.get("messages") or []
            if not msgs: return None
            m = svc.users().messages().get(userId="me", id=msgs[0]["id"], format="metadata",
                                            metadataHeaders=["Date","To","Subject"]).execute()
            headers = (m.get("payload") or {}).get("headers") or []
            def _h(name):
                for h in headers:
                    if (h.get("name") or "").lower() == name.lower():
                        return h.get("value")
                return None
            internal_ms = m.get("internalDate")
            sent_at = (datetime.fromtimestamp(int(internal_ms)/1000, tz=timezone.utc).isoformat()
                       if internal_ms else None)
            return {"to": _h("To"), "subject": _h("Subject"), "sent_at": sent_at}
        except Exception as e:
            return f"error: {e}"

    after_30d = int((datetime.now(timezone.utc) - timedelta(days=30)).timestamp())
    after_90d = int((datetime.now(timezone.utc) - timedelta(days=90)).timestamp())

    return jsonify({
        "email": email,
        "enviados_30d_TODOS":           _count(f"in:sent after:{after_30d}"),
        "enviados_30d_externos":        _count(f"in:sent after:{after_30d} -to:@grupoavantex.com"),
        "enviados_30d_internos":        _count(f"in:sent after:{after_30d} to:@grupoavantex.com"),
        "enviados_90d_TODOS":           _count(f"in:sent after:{after_90d}"),
        "enviados_90d_externos":        _count(f"in:sent after:{after_90d} -to:@grupoavantex.com"),
        "ultimo_enviado_30d":           _last_sent(f"in:sent after:{after_30d}"),
        "ultimo_enviado_externo_90d":   _last_sent(f"in:sent after:{after_90d} -to:@grupoavantex.com"),
    })


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

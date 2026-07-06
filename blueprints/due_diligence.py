# blueprints/due_diligence.py
"""
Due Diligence — Vista y encuestas NPS para cuentas adquiridas antes de
que tengan KAM asignado.

FEAT-2026-07-06 (Fugaci): Grupo Avantex está adquiriendo Fugaci (empresa
de fumigación en NL). Los ~130 clientes de Fugaci se cargan aquí para:
  1. Enviarles encuesta NPS con look & feel de Fugaci (SIN referencias
     a Avantex — el cliente aún no sabe de la operación).
  2. Cuando el CX Account Manager Pestex se contrate, promoverlos todos
     de un clic → pasan a ser cs_accounts normales con kam_id asignado.

Rutas admin (super_admin):
  GET  /cs/due-diligence                    → lista + acciones
  POST /cs/due-diligence/api/enviar-nps     → genera tokens + URLs para enviar
  POST /cs/due-diligence/api/promover       → bulk promoción a KAM
  GET  /cs/due-diligence/api/respuestas     → lista respuestas para analizar

Ruta pública (por token, sin login):
  GET  /dd-encuesta/<token>                 → encuesta con look Fugaci
  POST /dd-encuesta/<token>                 → recibe respuesta
"""
import secrets
from datetime import datetime, timezone

from flask import (
    Blueprint, render_template, request, jsonify, session, redirect,
    url_for, abort,
)

from extensions import db
from models import CSAccount, CSDDSurvey, UserCRM, RolCRM


dd_bp = Blueprint("due_diligence", __name__)


# ── Helpers ────────────────────────────────────────────────────────


def _es_super_admin() -> bool:
    return session.get("user_rol", "").lower().replace(" ", "_") == "super_admin"


def _genera_token() -> str:
    return secrets.token_urlsafe(24)


# ── Vista admin ────────────────────────────────────────────────────


@dd_bp.route("/cs/due-diligence")
def index():
    """Lista de cuentas en Due Diligence, con filtros por origen."""
    if not session.get("user_id"):
        return redirect(url_for("auth.login_page"))
    if not _es_super_admin():
        return "Acceso solo para Super Admin", 403

    origen = (request.args.get("origen") or "").strip() or None
    q = CSAccount.query.filter(CSAccount.en_due_diligence.is_(True))
    if origen:
        q = q.filter(CSAccount.origen_adquisicion == origen)
    cuentas = q.order_by(CSAccount.nombre).all()

    # KPIs agregados
    total_cuentas = len(cuentas)
    valor_mensual = 0.0
    valor_ytd = 0.0
    cxc_junio = 0.0
    for c in cuentas:
        m = (c.dd_metadata or {})
        try: valor_mensual += float(m.get("precio", 0) or 0)
        except (ValueError, TypeError): pass
        try: valor_ytd += float(m.get("facturacion_ytd", 0) or 0)
        except (ValueError, TypeError): pass
        try: cxc_junio += float(m.get("cxc_junio", 0) or 0)
        except (ValueError, TypeError): pass
    valor_anual = valor_mensual * 12

    # Respuestas de encuestas por account_id
    respuestas = {}
    if cuentas:
        for s in CSDDSurvey.query.filter(
            CSDDSurvey.account_id.in_([c.id for c in cuentas]),
        ).all():
            respuestas.setdefault(str(s.account_id), []).append(s)

    # Orígenes disponibles (para el selector)
    origenes = [r[0] for r in db.session.query(
        CSAccount.origen_adquisicion
    ).filter(
        CSAccount.en_due_diligence.is_(True),
        CSAccount.origen_adquisicion.isnot(None),
    ).distinct().all() if r[0]]

    # KAMs disponibles para asignación bulk
    kams = UserCRM.query.filter(
        UserCRM.rol == RolCRM.KAM,
        UserCRM.activo.is_(True),
    ).order_by(UserCRM.nombre).all()

    return render_template(
        "cs/cs_due_diligence.html",
        cuentas=cuentas,
        respuestas=respuestas,
        origenes=origenes,
        origen_filtro=origen,
        kams=kams,
        total_cuentas=total_cuentas,
        valor_mensual=valor_mensual,
        valor_anual=valor_anual,
        valor_ytd=valor_ytd,
        cxc_junio=cxc_junio,
        user_rol=session.get("user_rol", ""),
    )


@dd_bp.route("/cs/due-diligence/respuestas")
def respuestas_view():
    """Vista dedicada de respuestas + NPS Score calculado."""
    if not session.get("user_id"):
        return redirect(url_for("auth.login_page"))
    if not _es_super_admin():
        return "Acceso solo para Super Admin", 403
    origen = (request.args.get("origen") or "").strip() or None
    origenes = [r[0] for r in db.session.query(
        CSAccount.origen_adquisicion
    ).filter(
        CSAccount.en_due_diligence.is_(True),
        CSAccount.origen_adquisicion.isnot(None),
    ).distinct().all() if r[0]]
    return render_template(
        "cs/cs_dd_respuestas.html",
        origenes=origenes,
        origen_filtro=origen,
        user_rol=session.get("user_rol", ""),
    )


# ── Endpoints admin ────────────────────────────────────────────────


@dd_bp.route("/cs/due-diligence/api/generar-token", methods=["POST"])
def generar_token():
    """Genera (o recupera) el token de encuesta DD para una cuenta.
    Retorna la URL pública. Super Admin puede copiar/pegar al enviar por
    el canal que quiera (email, WhatsApp, etc.)."""
    if not _es_super_admin():
        return jsonify({"error": "Solo Super Admin"}), 403
    data = request.get_json() or {}
    account_id = data.get("account_id")
    if not account_id:
        return jsonify({"error": "account_id requerido"}), 400
    acc = db.session.get(CSAccount, account_id)
    if not acc or not acc.en_due_diligence:
        return jsonify({"error": "Cuenta no en Due Diligence"}), 404

    # Reusar el token más reciente sin respuesta, o crear uno nuevo
    survey = (CSDDSurvey.query
              .filter_by(account_id=acc.id, respondido_at=None)
              .order_by(CSDDSurvey.enviado_at.desc())
              .first())
    if not survey:
        survey = CSDDSurvey(
            account_id=acc.id,
            token=_genera_token(),
            contacto_email=(data.get("contacto_email") or "").strip() or None,
        )
        db.session.add(survey)
        db.session.commit()

    return jsonify({
        "ok": True,
        "token": survey.token,
        "url": url_for("due_diligence.encuesta_publica", token=survey.token, _external=True),
        "account_nombre": acc.nombre,
    })


@dd_bp.route("/cs/due-diligence/api/promover", methods=["POST"])
def promover_a_activas():
    """Bulk: promueve TODAS las cuentas Due Diligence del origen indicado
    al KAM especificado. Marca en_due_diligence=False.

    Body: {kam_id, origen}
    """
    if not _es_super_admin():
        return jsonify({"error": "Solo Super Admin"}), 403
    data = request.get_json() or {}
    kam_id = data.get("kam_id")
    origen = data.get("origen") or None

    kam = db.session.get(UserCRM, kam_id) if kam_id else None
    if not kam or kam.rol != RolCRM.KAM:
        return jsonify({"error": "kam_id inválido o no es KAM"}), 400

    q = CSAccount.query.filter(CSAccount.en_due_diligence.is_(True))
    if origen:
        q = q.filter(CSAccount.origen_adquisicion == origen)
    cuentas = q.all()
    n = 0
    for c in cuentas:
        c.kam_id = kam.id
        c.en_due_diligence = False
        n += 1
    db.session.commit()

    return jsonify({
        "ok": True,
        "cuentas_promovidas": n,
        "kam": kam.nombre,
        "origen": origen,
    })


@dd_bp.route("/cs/due-diligence/api/seed-fugaci", methods=["POST"])
def seed_fugaci_endpoint():
    """FEAT-2026-07-06: dispara el seed de los 130 clientes Fugaci
    desde el navegador (super_admin). Idempotente — si ya existen,
    actualiza. Reemplaza tener que abrir Terminal o Render Shell."""
    if not _es_super_admin():
        return jsonify({"error": "Solo Super Admin"}), 403
    try:
        # Import diferido para no tener que reiniciar si el archivo cambia
        import importlib, _seed_fugaci
        importlib.reload(_seed_fugaci)

        creadas = actualizadas = 0
        for row in _seed_fugaci.CLIENTES:
            (nombre, precio, tipo_cliente, contacto, fact_ytd, cxc,
             comport, visitas, tiempo, tecnicos) = row
            nombre = _seed_fugaci._clean(nombre)
            metadata = {
                "precio": precio,
                "tipo_cliente": tipo_cliente,
                "contacto_fugaci": _seed_fugaci._clean(contacto),
                "facturacion_ytd": fact_ytd,
                "cxc_junio": cxc,
                "comportamiento_pago": _seed_fugaci._clean(comport),
                "visitas_mes": visitas,
                "tiempo_visita": _seed_fugaci._clean(tiempo),
                "tecnicos": tecnicos,
                "contrato_vigente": True,
            }
            existing = CSAccount.query.filter_by(nombre=nombre).first()
            if existing:
                existing.en_due_diligence = True
                existing.origen_adquisicion = "Fugaci"
                existing.dd_metadata = metadata
                existing.unidades_contratadas = existing.unidades_contratadas or "PESTEX"
                actualizadas += 1
            else:
                acc = CSAccount(
                    nombre=nombre, kam_id=None,
                    en_due_diligence=True, origen_adquisicion="Fugaci",
                    dd_metadata=metadata, unidades_contratadas="PESTEX",
                    mrr=0, mrr_observado=0, arr_proyectado=0, sucursales=0,
                )
                db.session.add(acc)
                creadas += 1
        db.session.commit()
        total = CSAccount.query.filter_by(en_due_diligence=True).count()
        return jsonify({
            "ok": True,
            "creadas": creadas,
            "actualizadas": actualizadas,
            "total_dd_ahora": total,
        })
    except Exception as e:
        db.session.rollback()
        import traceback
        return jsonify({
            "error": f"{type(e).__name__}: {e}",
            "traceback": traceback.format_exc()[-800:],
        }), 500


@dd_bp.route("/cs/due-diligence/api/respuestas", methods=["GET"])
def respuestas_json():
    """Lista todas las respuestas de encuestas DD + agregado NPS.

    FEAT-2026-07-06: retorna 3 secciones:
      - resumen: NPS Score, distribución (Promotores/Pasivos/Detractores),
                 tasa de respuesta, promedio satisfacción, distribución
                 de continuidad.
      - respuestas: array con cada respuesta detallada (incluye survey_id).
      - por_cuenta: {account_id: [respuestas]} para lookup rápido en UI.
    """
    if not _es_super_admin():
        return jsonify({"error": "Solo Super Admin"}), 403
    origen = (request.args.get("origen") or "").strip() or None
    q = (db.session.query(CSDDSurvey, CSAccount)
         .join(CSAccount, CSAccount.id == CSDDSurvey.account_id))
    if origen:
        q = q.filter(CSAccount.origen_adquisicion == origen)

    all_rows = q.order_by(CSDDSurvey.respondido_at.desc().nullslast(),
                          CSDDSurvey.enviado_at.desc()).all()

    respuestas = []
    por_cuenta = {}
    enviadas = 0
    respondidas = 0
    promotores = pasivos = detractores = 0
    sat_vals = []
    cont_dist = {"Si": 0, "Si con cambios": 0, "No": 0}

    for s, a in all_rows:
        enviadas += 1
        item = {
            "survey_id":        str(s.id),
            "account_id":       str(a.id),
            "cuenta_nombre":    a.nombre,
            "origen":           a.origen_adquisicion,
            "enviado_at":       s.enviado_at.isoformat() if s.enviado_at else None,
            "respondido_at":    s.respondido_at.isoformat() if s.respondido_at else None,
            "nps":              s.nps,
            "satisfaccion":     s.satisfaccion,
            "continuidad":      s.continuidad,
            "preocupaciones":   s.preocupaciones,
            "areas_mejora":     s.areas_mejora,
            "contacto_nombre":  s.contacto_nombre,
            "contacto_puesto":  s.contacto_puesto,
            "contacto_email":   s.contacto_email,
        }
        respuestas.append(item)
        por_cuenta.setdefault(str(a.id), []).append(item)
        if s.respondido_at:
            respondidas += 1
            if s.nps is not None:
                if s.nps >= 9:   promotores  += 1
                elif s.nps >= 7: pasivos     += 1
                else:            detractores += 1
            if s.satisfaccion is not None:
                sat_vals.append(s.satisfaccion)
            if s.continuidad in cont_dist:
                cont_dist[s.continuidad] += 1

    # NPS Score = (%Promotores - %Detractores) sobre respuestas con NPS
    con_nps = promotores + pasivos + detractores
    nps_score = round(((promotores - detractores) / con_nps * 100), 1) if con_nps else None
    tasa_respuesta = round(respondidas / enviadas * 100, 1) if enviadas else 0.0
    sat_promedio = round(sum(sat_vals) / len(sat_vals), 1) if sat_vals else None

    return jsonify({
        "total": len(respuestas),
        "resumen": {
            "enviadas":            enviadas,
            "respondidas":         respondidas,
            "tasa_respuesta_pct":  tasa_respuesta,
            "nps_score":           nps_score,
            "promotores":          promotores,
            "pasivos":             pasivos,
            "detractores":         detractores,
            "sat_promedio":        sat_promedio,
            "continuidad_dist":    cont_dist,
        },
        "respuestas": respuestas,
        "por_cuenta": por_cuenta,
    })


# ── Endpoints PÚBLICOS (por token, sin login) ──────────────────────
# CRÍTICO: NO deben mostrar ninguna referencia a Avantex / Grupo Avantex
# / Pestex. Solo look & feel de Fugaci (verde oscuro).


@dd_bp.route("/dd-encuesta/<token>", methods=["GET"])
def encuesta_publica(token):
    """Muestra el formulario NPS con look Fugaci."""
    survey = CSDDSurvey.query.filter_by(token=token).first()
    if not survey:
        abort(404)
    ya_respondida = survey.respondido_at is not None
    return render_template(
        "cs/dd_encuesta_fugaci.html",
        survey=survey,
        ya_respondida=ya_respondida,
    )


@dd_bp.route("/dd-encuesta/<token>", methods=["POST"])
def enviar_respuesta(token):
    """Recibe respuesta. Idempotente: si ya respondió, ignora."""
    survey = CSDDSurvey.query.filter_by(token=token).first()
    if not survey:
        abort(404)
    if survey.respondido_at is not None:
        return render_template("cs/dd_encuesta_fugaci.html",
                               survey=survey, ya_respondida=True)

    data = request.form
    try:
        nps_v = int(data.get("nps") or 0)
        if 0 <= nps_v <= 10:
            survey.nps = nps_v
    except (ValueError, TypeError):
        pass
    try:
        sat_v = int(data.get("satisfaccion") or 0)
        if 1 <= sat_v <= 5:
            survey.satisfaccion = sat_v
    except (ValueError, TypeError):
        pass
    cont = (data.get("continuidad") or "").strip()
    if cont in ("Si", "Si con cambios", "No"):
        survey.continuidad = cont
    survey.preocupaciones  = (data.get("preocupaciones") or "").strip() or None
    survey.areas_mejora    = (data.get("areas_mejora") or "").strip() or None
    survey.contacto_nombre = (data.get("contacto_nombre") or "").strip() or None
    survey.contacto_puesto = (data.get("contacto_puesto") or "").strip() or None
    survey.respondido_at   = datetime.now(timezone.utc)
    db.session.commit()

    return render_template("cs/dd_encuesta_fugaci.html",
                           survey=survey, ya_respondida=True,
                           acabo_de_responder=True)

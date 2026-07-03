# cs_health_score.py
"""
Health Score v3 — 6 componentes, sin métricas manuales.
Score: 0-100 → Sana(70+), Atención(40-69), Riesgo(0-39)

Componentes:
  1. Cobranza (25%)       — % pagado vs facturado (solo AROMATEX + PESTEX)
  2. Operación (25%)      — ratio citas completadas / relevantes
  3. NPS (20%)            — automático desde encuestas. Sin respuesta = 0
  4. CSAT (15%)           — automático desde encuestas (promedio 6 dimensiones). Sin respuesta = 0
  5. Vencido (10%)        — % de facturación NO vencida (100 = nada vencido, 0 = todo vencido)
  6. Email Response (5%)  — tiempo de respuesta del KAM a emails del cliente (mediana 30d)
                            Sin datos = 50 (neutral). ≤2h=100, ≤8h=80, ≤24h=50, ≤48h=20, >48h=0
"""
from datetime import date, datetime, timedelta, timezone
from sqlalchemy import func, case
from extensions import db
from models import CSAccount, CSInvoice, CSAppointment, CSEncuesta, KAMEmailResponse


def _preload_cita_stats(account_ids):
    if not account_ids:
        return {}
    rows = (
        db.session.query(
            CSAppointment.account_id,
            CSAppointment.estatus,
            func.count(CSAppointment.id),
        )
        .filter(CSAppointment.account_id.in_(account_ids))
        .group_by(CSAppointment.account_id, CSAppointment.estatus)
        .all()
    )
    stats = {}
    for acc_id, estatus, cnt in rows:
        key = str(acc_id)
        if key not in stats:
            stats[key] = {"completadas": 0, "relevantes": 0}
        if estatus == "Terminada":
            stats[key]["completadas"] += cnt
        if estatus not in ("Cancelada", "Servicio Duplicado"):
            stats[key]["relevantes"] += cnt
    return stats


def _preload_cobranza(account_ids):
    """Cobranza solo de UENs AROMATEX y PESTEX."""
    if not account_ids:
        return {}
    hoy = date.today()
    rows = (
        db.session.query(
            CSInvoice.account_id,
            func.coalesce(func.sum(CSInvoice.total), 0),
            func.coalesce(func.sum(CSInvoice.pagado), 0),
            # Vencido: pendiente de facturas cuya fecha_vencimiento ya pasó
            func.coalesce(func.sum(case(
                (db.and_(CSInvoice.pendiente > 0, CSInvoice.fecha_vencimiento < hoy), CSInvoice.pendiente),
                else_=0,
            )), 0),
        )
        .filter(
            CSInvoice.account_id.in_(account_ids),
            db.or_(
                CSInvoice.uen.ilike("%AROMATEX%"),
                CSInvoice.uen.ilike("%PESTEX%"),
            ),
        )
        .group_by(CSInvoice.account_id)
        .all()
    )
    return {
        str(r[0]): {"facturado": float(r[1]), "pagado": float(r[2]), "vencido": float(r[3])}
        for r in rows
    }


def _preload_encuestas(account_ids):
    """Promedio NPS y CSAT desde encuestas."""
    if not account_ids:
        return {}
    rows = (
        db.session.query(
            CSEncuesta.account_id,
            func.avg(CSEncuesta.nps),
            func.avg(CSEncuesta.csat),
            func.avg(CSEncuesta.csat_calidad),
            func.avg(CSEncuesta.csat_respuesta),
            func.avg(CSEncuesta.csat_comunicacion),
            func.avg(CSEncuesta.csat_precio),
            func.avg(CSEncuesta.csat_tecnico),
        )
        .filter(CSEncuesta.account_id.in_(account_ids))
        .group_by(CSEncuesta.account_id)
        .all()
    )
    result = {}
    for r in rows:
        key = str(r[0])
        avg_nps = float(r[1]) if r[1] is not None else None
        csat_vals = [float(v) for v in r[2:] if v is not None]
        avg_csat = sum(csat_vals) / len(csat_vals) if csat_vals else None
        result[key] = {"nps": avg_nps, "csat": avg_csat}
    return result


def _preload_email_response(account_ids):
    """Mediana de response_hours por cuenta CS en los últimos 30 días."""
    if not account_ids:
        return {}
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    rows = (
        db.session.query(
            KAMEmailResponse.account_id,
            func.percentile_cont(0.5).within_group(
                KAMEmailResponse.response_hours
            ).label("median_hours"),
        )
        .filter(
            KAMEmailResponse.account_id.in_(account_ids),
            KAMEmailResponse.replied_at >= cutoff,
        )
        .group_by(KAMEmailResponse.account_id)
        .all()
    )
    return {str(r[0]): float(r[1]) for r in rows if r[1] is not None}


def calcular_health_scores_batch(accounts: list[CSAccount]) -> dict:
    account_ids = [a.id for a in accounts]
    cita_stats    = _preload_cita_stats(account_ids)
    cobranza_map  = _preload_cobranza(account_ids)
    encuesta_map  = _preload_encuestas(account_ids)
    email_map     = _preload_email_response(account_ids)

    results = {}
    for acc in accounts:
        results[str(acc.id)] = _calcular_score(acc, cita_stats, cobranza_map, encuesta_map, email_map)
    return results


def calcular_health_score(account: CSAccount) -> dict:
    cita_stats   = _preload_cita_stats([account.id])
    cobranza_map = _preload_cobranza([account.id])
    encuesta_map = _preload_encuestas([account.id])
    email_map    = _preload_email_response([account.id])
    return _calcular_score(account, cita_stats, cobranza_map, encuesta_map, email_map)


def _calcular_score(account, cita_stats, cobranza_map, encuesta_map, email_map=None):
    desglose = {}
    key = str(account.id)

    # ── 1. COBRANZA (25%) — solo AROMATEX + PESTEX ──
    cobr = cobranza_map.get(key, {"facturado": 0, "pagado": 0, "vencido": 0})
    facturado = cobr["facturado"]
    pagado = cobr["pagado"]
    pct_pagado = pagado / facturado if facturado > 0 else 0.5
    score_cobranza = min(pct_pagado * 100, 100)
    desglose["cobranza"] = {
        "peso": 25, "score": round(score_cobranza, 1),
        "detalle": f"{pct_pagado:.1%} pagado (${pagado:,.0f} / ${facturado:,.0f}) — solo AROMATEX+PESTEX",
    }

    # ── 2. OPERACIÓN (25%) ──
    stats = cita_stats.get(key, {"completadas": 0, "relevantes": 0})
    completadas = stats["completadas"]
    relevantes = stats["relevantes"]
    ratio_citas = completadas / relevantes if relevantes > 0 else 0.5
    score_operacion = ratio_citas * 100
    desglose["operacion"] = {
        "peso": 25, "score": round(score_operacion, 1),
        "detalle": f"{completadas}/{relevantes} citas completadas ({ratio_citas:.1%})",
    }

    # ── 3. NPS (20%) — automático desde encuestas, sin dato = 0 ──
    enc = encuesta_map.get(key, {"nps": None, "csat": None})
    nps_val = enc["nps"]
    if nps_val is not None:
        score_nps = nps_val * 10  # 0-10 → 0-100
        nps_detalle = f"NPS {nps_val:.1f}/10 — {'Promotor' if nps_val >= 9 else 'Pasivo' if nps_val >= 7 else 'Detractor'}"
    else:
        score_nps = 0
        nps_detalle = "Sin respuestas de encuesta (score 0)"
    desglose["nps"] = {
        "peso": 20, "score": round(min(score_nps, 100), 1),
        "detalle": nps_detalle,
    }

    # ── 4. CSAT (15%) — automático, sin dato = 0 ──
    csat_val = enc["csat"]
    if csat_val is not None:
        score_csat = (csat_val - 1) / 4 * 100  # 1-5 → 0-100
        csat_detalle = f"CSAT {csat_val:.1f}/5 — promedio 6 dimensiones"
    else:
        score_csat = 0
        csat_detalle = "Sin respuestas de encuesta (score 0)"
    desglose["csat"] = {
        "peso": 15, "score": round(min(score_csat, 100), 1),
        "detalle": csat_detalle,
    }

    # ── 5. VENCIDO (10%) — % NO vencido (100 = nada vencido) ──
    vencido = cobr["vencido"]
    if facturado > 0:
        pct_no_vencido = 1 - (vencido / facturado)
        score_vencido = max(pct_no_vencido * 100, 0)
        vencido_detalle = f"${vencido:,.0f} vencido de ${facturado:,.0f} ({(1-pct_no_vencido):.1%} vencido)"
    else:
        score_vencido = 100
        vencido_detalle = "Sin facturación registrada"
    desglose["vencido"] = {
        "peso": 10, "score": round(score_vencido, 1),
        "detalle": vencido_detalle,
    }

    # ── 6. EMAIL RESPONSE (5%) — mediana de respuesta del KAM últimos 30d ──
    median_h = (email_map or {}).get(key)
    if median_h is None:
        score_email = 50  # neutral — sin datos correlacionados aún
        email_detalle = "Sin emails correlacionados con esta cuenta"
    elif median_h <= 2:
        score_email = 100
        email_detalle = f"Mediana {median_h:.1f}h — Excelente (≤2h)"
    elif median_h <= 8:
        score_email = 80
        email_detalle = f"Mediana {median_h:.1f}h — Bueno (≤8h)"
    elif median_h <= 24:
        score_email = 50
        email_detalle = f"Mediana {median_h:.1f}h — Aceptable (≤24h)"
    elif median_h <= 48:
        score_email = 20
        email_detalle = f"Mediana {median_h:.1f}h — Lento (≤48h)"
    else:
        score_email = 0
        email_detalle = f"Mediana {median_h:.1f}h — Muy lento (>48h)"
    desglose["email_response"] = {
        "peso": 5, "score": round(score_email, 1),
        "detalle": email_detalle,
    }

    # ── TOTAL ──
    total = (
        score_cobranza * 0.25 +
        score_operacion * 0.25 +
        min(score_nps, 100) * 0.20 +
        min(score_csat, 100) * 0.15 +
        score_vencido * 0.10 +
        score_email * 0.05
    )
    cat = "Sana" if total >= 70 else "Atención" if total >= 40 else "Riesgo"
    color = "green" if total >= 70 else "yellow" if total >= 40 else "red"

    return {"score": round(total, 1), "categoria": cat, "color": color, "desglose": desglose}

# cs_health_score.py
"""
Health Score v4 — 5 componentes con RE-NORMALIZACIÓN de pesos.
Score: 0-100 → Sana(70+), Atención(40-69), Riesgo(0-39). None = sin datos.

Componentes:
  1. Cobranza (25%)       — % pagado vs facturado. Sin factura → EXCLUIDO
  2. Operación (25%)      — ratio citas completadas / relevantes. Sin citas → EXCLUIDO
  3. Evaluaciones (35%)   — FUSIÓN NPS+CSAT (promedio de los disponibles). Sin encuesta → EXCLUIDO
  4. Vencido (10%)        — % de facturación NO vencida. Sin factura → EXCLUIDO
  5. Email Response (5%)  — tiempo mediano de respuesta del KAM (≤2h=100, ≤8h=80, ≤24h=60,
                            ≤48h=30, >48h=0). Sin datos → EXCLUIDO

FIX-2026-07-03: v3 tenía defaults neutrales (50) para componentes sin datos, lo que
planchaba todos los scores cerca de 50. v4 EXCLUYE componentes sin datos y re-normaliza
los pesos restantes. Así una cuenta con solo cobranza al 85% sale con 85, no ~40.
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
    """FIX-2026-07-03: re-normaliza pesos cuando falta un componente.

    Antes: defaults neutrales (50) inflaban artificialmente cuentas sin datos,
    planchando todo cerca de 50. Ahora: componentes sin datos se EXCLUYEN del
    total y los pesos restantes se re-normalizan sobre lo que sí existe.

    También: NPS+CSAT fusionados en un solo componente 'Evaluaciones' (35%)
    que promedia los dos scores (o toma el único disponible si solo hay uno).
    """
    desglose = {}
    key = str(account.id)
    componentes = []  # lista de (nombre, score_0_100, peso_base)

    # ── 1. COBRANZA (25%) — solo AROMATEX + PESTEX ──
    cobr = cobranza_map.get(key, {"facturado": 0, "pagado": 0, "vencido": 0})
    facturado = cobr["facturado"]
    pagado = cobr["pagado"]
    if facturado > 0:
        pct_pagado = pagado / facturado
        score_cobranza = min(pct_pagado * 100, 100)
        componentes.append(("cobranza", score_cobranza, 25))
        desglose["cobranza"] = {
            "peso": 25, "score": round(score_cobranza, 1),
            "detalle": f"{pct_pagado:.1%} pagado (${pagado:,.0f} / ${facturado:,.0f})",
        }
    else:
        desglose["cobranza"] = {
            "peso": 0, "score": None,
            "detalle": "Sin facturación registrada — excluido del score",
        }

    # ── 2. OPERACIÓN (25%) ──
    stats = cita_stats.get(key, {"completadas": 0, "relevantes": 0})
    completadas = stats["completadas"]
    relevantes = stats["relevantes"]
    if relevantes > 0:
        ratio_citas = completadas / relevantes
        score_operacion = ratio_citas * 100
        componentes.append(("operacion", score_operacion, 25))
        desglose["operacion"] = {
            "peso": 25, "score": round(score_operacion, 1),
            "detalle": f"{completadas}/{relevantes} citas completadas ({ratio_citas:.1%})",
        }
    else:
        desglose["operacion"] = {
            "peso": 0, "score": None,
            "detalle": "Sin citas relevantes — excluido del score",
        }

    # ── 3. EVALUACIONES (35%) — FUSIÓN NPS + CSAT ──
    # FIX-2026-07-03 (Diego): antes NPS (20%) y CSAT (15%) eran componentes
    # separados. Ahora se promedian en un solo score. Si solo hay uno de los
    # dos, se usa ese. Si no hay ninguno → excluido con re-normalización.
    enc = encuesta_map.get(key, {"nps": None, "csat": None})
    nps_val = enc["nps"]
    csat_val = enc["csat"]
    partes_score = []
    partes_detalle = []
    if nps_val is not None:
        s_nps = min(nps_val * 10, 100)  # 0-10 → 0-100
        partes_score.append(s_nps)
        etiqueta = "Promotor" if nps_val >= 9 else "Pasivo" if nps_val >= 7 else "Detractor"
        partes_detalle.append(f"NPS {nps_val:.1f}/10 ({etiqueta})")
    if csat_val is not None:
        s_csat = min((csat_val - 1) / 4 * 100, 100)  # 1-5 → 0-100
        partes_score.append(s_csat)
        partes_detalle.append(f"CSAT {csat_val:.1f}/5 (6 dimensiones)")
    if partes_score:
        score_eval = sum(partes_score) / len(partes_score)
        componentes.append(("evaluaciones", score_eval, 35))
        desglose["evaluaciones"] = {
            "peso": 35, "score": round(score_eval, 1),
            "detalle": " · ".join(partes_detalle),
        }
    else:
        desglose["evaluaciones"] = {
            "peso": 0, "score": None,
            "detalle": "Sin encuestas respondidas — excluido del score",
        }

    # ── 4. VENCIDO (10%) — % NO vencido ──
    vencido = cobr["vencido"]
    if facturado > 0:
        pct_no_vencido = 1 - (vencido / facturado)
        score_vencido = max(pct_no_vencido * 100, 0)
        componentes.append(("vencido", score_vencido, 10))
        desglose["vencido"] = {
            "peso": 10, "score": round(score_vencido, 1),
            "detalle": f"${vencido:,.0f} vencido de ${facturado:,.0f} ({(1-pct_no_vencido):.1%} vencido)",
        }
    else:
        desglose["vencido"] = {
            "peso": 0, "score": None,
            "detalle": "Sin facturación — excluido del score",
        }

    # ── 5. EMAIL RESPONSE (5%) — mediana de respuesta del KAM últimos 30d ──
    median_h = (email_map or {}).get(key)
    if median_h is not None:
        if median_h <= 2:    score_email = 100
        elif median_h <= 8:  score_email = 80
        elif median_h <= 24: score_email = 60
        elif median_h <= 48: score_email = 30
        else:                score_email = 0
        componentes.append(("email_response", score_email, 5))
        desglose["email_response"] = {
            "peso": 5, "score": round(score_email, 1),
            "detalle": f"Mediana {median_h:.1f}h",
        }
    else:
        desglose["email_response"] = {
            "peso": 0, "score": None,
            "detalle": "Sin emails correlacionados — excluido del score",
        }

    # ── TOTAL con RE-NORMALIZACIÓN ──
    # Los componentes sin datos se EXCLUYEN. El peso total se re-normaliza
    # sobre los componentes con datos, así una cuenta con solo cobranza al
    # 85% sale con score 85 (no ~40 planchado por defaults neutrales).
    if componentes:
        peso_total = sum(c[2] for c in componentes)
        total = sum(c[1] * c[2] for c in componentes) / peso_total
        cat = "Sana" if total >= 70 else "Atención" if total >= 40 else "Riesgo"
        color = "green" if total >= 70 else "yellow" if total >= 40 else "red"
    else:
        total = None
        cat = "Sin datos"
        color = "gray"

    return {
        "score": round(total, 1) if total is not None else None,
        "categoria": cat,
        "color": color,
        "desglose": desglose,
        # FIX-2026-07-03: qué componentes participaron y cuánto peso total
        # cubrieron (útil para saber si el score es representativo).
        "componentes_con_datos": [c[0] for c in componentes],
        "cobertura_peso": sum(c[2] for c in componentes),
    }

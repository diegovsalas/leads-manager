# cs_health_score.py
"""
Health Score para CS Dashboard — optimizado con queries batch.
Score: 0-100 → Sana(70+), Atención(40-69), Riesgo(0-39)
"""
from datetime import date
from sqlalchemy import func
from extensions import db
from models import CSAccount, CSInvoice, CSAppointment


def _preload_cita_stats(account_ids):
    """Carga stats de citas en batch: {account_id: {completadas, relevantes}}"""
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


def _preload_recencia(account_ids):
    """Última fecha de pago por cuenta: {account_id: date}"""
    if not account_ids:
        return {}

    rows = (
        db.session.query(
            CSInvoice.account_id,
            func.max(CSInvoice.fecha_pago),
        )
        .filter(
            CSInvoice.account_id.in_(account_ids),
            CSInvoice.fecha_pago.isnot(None),
        )
        .group_by(CSInvoice.account_id)
        .all()
    )

    return {str(acc_id): fecha for acc_id, fecha in rows}


def calcular_health_scores_batch(accounts: list[CSAccount]) -> dict:
    """Calcula health scores para múltiples cuentas con solo 2 queries."""
    account_ids = [a.id for a in accounts]
    cita_stats = _preload_cita_stats(account_ids)
    recencia_map = _preload_recencia(account_ids)

    results = {}
    for acc in accounts:
        results[str(acc.id)] = _calcular_score(acc, cita_stats, recencia_map)
    return results


def calcular_health_score(account: CSAccount) -> dict:
    """Calcula health score para una sola cuenta (usa batch internamente)."""
    cita_stats = _preload_cita_stats([account.id])
    recencia_map = _preload_recencia([account.id])
    return _calcular_score(account, cita_stats, recencia_map)


def _calcular_score(account, cita_stats, recencia_map):
    desglose = {}
    facturacion = float(account.facturacion_q1 or 0)
    pagado = float(account.pagado_q1 or 0)

    # 1. COBRANZA (20%)
    pct_pagado = pagado / facturacion if facturacion > 0 else 0.5
    score_cobranza = min(pct_pagado * 100, 100)
    desglose["cobranza"] = {
        "peso": 20, "score": round(score_cobranza, 1),
        "detalle": f"{pct_pagado:.1%} pagado (${pagado:,.0f} / ${facturacion:,.0f})",
    }

    # 2. UNs (10%)
    uns = [u.strip() for u in (account.unidades_contratadas or "").split(",") if u.strip()]
    score_uns = (len(uns) / 2) * 100
    desglose["uns"] = {
        "peso": 10, "score": round(score_uns, 1),
        "detalle": f'{len(uns)} UN(s): {", ".join(uns) if uns else "ninguna"}',
    }

    # 3. OPERACIÓN (15%) — from preloaded stats
    key = str(account.id)
    stats = cita_stats.get(key, {"completadas": 0, "relevantes": 0})
    completadas = stats["completadas"]
    relevantes = stats["relevantes"]
    ratio_citas = completadas / relevantes if relevantes > 0 else 0.5
    score_operacion = ratio_citas * 100
    desglose["operacion"] = {
        "peso": 15, "score": round(score_operacion, 1),
        "detalle": f"{completadas}/{relevantes} citas completadas ({ratio_citas:.1%})",
    }

    # 4. RECENCIA (10%) — from preloaded
    hoy = date.today()
    ultima_pago = recencia_map.get(key)
    if ultima_pago:
        dias = (hoy - ultima_pago).days
        score_recencia = 100 if dias <= 30 else 70 if dias <= 60 else 40 if dias <= 90 else 10
    else:
        dias = None
        score_recencia = 10
    desglose["recencia"] = {
        "peso": 10, "score": round(score_recencia, 1),
        "detalle": f"{dias} días desde último pago" if dias is not None else "Sin pagos registrados",
    }

    # 5. NPS (15%)
    if account.nps is not None:
        if account.nps >= 9: score_nps, nps_cat = 100, "Promotor"
        elif account.nps >= 7: score_nps, nps_cat = 60, "Pasivo"
        else: score_nps, nps_cat = 20, "Detractor"
        nps_detalle = f"NPS {account.nps:.0f}/10 — {nps_cat}"
    else:
        score_nps, nps_detalle = 50, "Sin dato (neutro)"
    desglose["nps"] = {"peso": 15, "score": round(score_nps, 1), "detalle": nps_detalle}

    # 6. PULSO (15%)
    pulso_map = {"Sana": 100, "Atención": 50, "Riesgo": 10}
    if account.pulso and account.pulso in pulso_map:
        score_pulso = pulso_map[account.pulso]
        pulso_detalle = f"Pulso: {account.pulso}"
    else:
        score_pulso, pulso_detalle = 50, "Sin dato (neutro)"
    desglose["pulso"] = {"peso": 15, "score": round(score_pulso, 1), "detalle": pulso_detalle}

    # 7. EFICIENCIA (15%)
    if account.eficiencia_operativa is not None:
        ef = account.eficiencia_operativa
        if ef >= 95: score_ef, ef_cat = 100, "Sana"
        elif ef >= 90: score_ef, ef_cat = 60, "Atención"
        else: score_ef, ef_cat = 20, "Riesgo"
        ef_detalle = f"{ef:.1f}% — {ef_cat}"
    else:
        score_ef, ef_detalle = 50, "Sin dato (neutro)"
    desglose["eficiencia"] = {"peso": 15, "score": round(score_ef, 1), "detalle": ef_detalle}

    # TOTAL
    total = (
        score_cobranza * 0.20 + score_uns * 0.10 + score_operacion * 0.15 +
        score_recencia * 0.10 + score_nps * 0.15 + score_pulso * 0.15 + score_ef * 0.15
    )
    cat = "Sana" if total >= 70 else "Atención" if total >= 40 else "Riesgo"
    color = "green" if total >= 70 else "yellow" if total >= 40 else "red"

    return {"score": round(total, 1), "categoria": cat, "color": color, "desglose": desglose}

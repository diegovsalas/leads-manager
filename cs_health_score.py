# cs_health_score.py
"""
Health Score para CS Dashboard — adaptado a PostgreSQL/UUID.
Score: 0-100 → Sana(70+), Atención(40-69), Riesgo(0-39)
"""
from datetime import date
from models import CSAccount, CSInvoice, CSAppointment


def calcular_health_score(account: CSAccount) -> dict:
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

    # 3. OPERACIÓN (15%)
    citas = CSAppointment.query.filter_by(account_id=account.id).all()
    completadas = sum(1 for c in citas if c.estatus == "Terminada")
    relevantes = sum(1 for c in citas if c.estatus not in ("Cancelada", "Servicio Duplicado"))
    ratio_citas = completadas / relevantes if relevantes > 0 else 0.5
    score_operacion = ratio_citas * 100
    desglose["operacion"] = {
        "peso": 15, "score": round(score_operacion, 1),
        "detalle": f"{completadas}/{relevantes} citas completadas ({ratio_citas:.1%})",
    }

    # 4. RECENCIA (10%)
    ultima_pagada = (
        CSInvoice.query.filter_by(account_id=account.id)
        .filter(CSInvoice.fecha_pago.isnot(None))
        .order_by(CSInvoice.fecha_pago.desc()).first()
    )
    hoy = date.today()
    if ultima_pagada and ultima_pagada.fecha_pago:
        dias = (hoy - ultima_pagada.fecha_pago).days
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

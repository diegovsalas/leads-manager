# cs_alerts.py
"""Motor de alertas para CS Dashboard — adaptado a PostgreSQL."""
from datetime import datetime, date, timedelta
from models import CSAccount, CSTask, CSAppointment
from cs_health_score import calcular_health_score


def generar_alertas() -> list[dict]:
    alertas = []
    hoy = date.today()
    accounts = CSAccount.query.all()

    for acc in accounts:
        hs = calcular_health_score(acc)
        score = hs["score"]
        categoria = hs["categoria"]
        tareas_abiertas = CSTask.query.filter_by(account_id=acc.id, completada=False).all()
        tipos_tareas = set(t.tipo for t in tareas_abiertas)

        # Regla 1: Cuenta nueva sin QBR
        if acc.es_cuenta_nueva:
            tiene_qbr = "QBR" in tipos_tareas
            qbr_completado = CSTask.query.filter_by(account_id=acc.id, tipo="QBR", completada=True).first()
            if not tiene_qbr and not qbr_completado:
                alertas.append({
                    "cuenta": acc.nombre, "account_id": str(acc.id),
                    "kam": acc.kam.nombre if acc.kam else "Sin KAM",
                    "tipo": "sin_qbr", "titulo": "Cuenta nueva sin QBR agendado",
                    "detalle": f"{acc.nombre} es cuenta nueva sin QBR.",
                    "severidad": "alta", "accion": "Agendar QBR con el cliente",
                })

        # Regla 2: >30% pendiente cobranza
        facturacion = float(acc.facturacion_q1 or 0)
        pendiente = float(acc.pendiente_q1 or 0)
        if facturacion > 0:
            pct_pendiente = pendiente / facturacion
            if pct_pendiente > 0.30:
                alertas.append({
                    "cuenta": acc.nombre, "account_id": str(acc.id),
                    "kam": acc.kam.nombre if acc.kam else "Sin KAM",
                    "tipo": "cobranza_alta",
                    "titulo": f"{pct_pendiente:.0%} pendiente de cobranza",
                    "detalle": f"${pendiente:,.0f} pendiente de ${facturacion:,.0f} facturado Q1.",
                    "severidad": "critica" if pct_pendiente > 0.60 else "alta",
                    "accion": "Seguimiento de cobranza con el cliente",
                })

        # Regla 3: Sin citas completadas en último mes
        hace_30 = datetime.now() - timedelta(days=30)
        citas_recientes = CSAppointment.query.filter_by(account_id=acc.id).filter(
            CSAppointment.fecha_inicio >= hace_30
        ).all()
        terminadas_recientes = sum(1 for c in citas_recientes if c.estatus == "Terminada")
        if len(citas_recientes) > 0 and terminadas_recientes == 0:
            alertas.append({
                "cuenta": acc.nombre, "account_id": str(acc.id),
                "kam": acc.kam.nombre if acc.kam else "Sin KAM",
                "tipo": "sin_citas_completadas",
                "titulo": "Sin citas completadas en último mes",
                "detalle": f"{len(citas_recientes)} citas sin terminar.",
                "severidad": "alta", "accion": "Revisar operación",
            })
        elif len(citas_recientes) == 0 and acc.sucursales > 0:
            alertas.append({
                "cuenta": acc.nombre, "account_id": str(acc.id),
                "kam": acc.kam.nombre if acc.kam else "Sin KAM",
                "tipo": "sin_actividad",
                "titulo": "Sin actividad operativa en último mes",
                "detalle": f"Sin citas en 30 días ({acc.sucursales} sucursales).",
                "severidad": "media", "accion": "Verificar calendario",
            })

        # Regla 4: Riesgo sin tarea
        if categoria == "Riesgo" and not tareas_abiertas:
            alertas.append({
                "cuenta": acc.nombre, "account_id": str(acc.id),
                "kam": acc.kam.nombre if acc.kam else "Sin KAM",
                "tipo": "riesgo_sin_tarea",
                "titulo": f"Cuenta en RIESGO (score {score}) sin plan",
                "detalle": f"Score {score}/100 sin tareas asignadas.",
                "severidad": "critica", "accion": "Crear plan de acción",
            })

        # Regla 5: Pre-riesgo
        if categoria == "Atención" and facturacion > 0:
            pct_pend = pendiente / facturacion
            if pct_pend > 0.50:
                alertas.append({
                    "cuenta": acc.nombre, "account_id": str(acc.id),
                    "kam": acc.kam.nombre if acc.kam else "Sin KAM",
                    "tipo": "pre_riesgo",
                    "titulo": f"Riesgo de caer a rojo (score {score})",
                    "detalle": f"{pct_pend:.0%} pendiente.",
                    "severidad": "alta", "accion": "Intervención preventiva",
                })

    sev_order = {"critica": 0, "alta": 1, "media": 2}
    alertas.sort(key=lambda a: (sev_order.get(a["severidad"], 9), a["cuenta"]))
    return alertas


def alertas_por_cuenta(account_id: str) -> list[dict]:
    return [a for a in generar_alertas() if a["account_id"] == account_id]

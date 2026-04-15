# cs_alerts.py
"""Motor de alertas para CS Dashboard — optimizado con batch queries."""
from datetime import datetime, date, timedelta
from sqlalchemy import func
from extensions import db
from models import CSAccount, CSTask, CSAppointment
from cs_health_score import calcular_health_scores_batch


def generar_alertas(accounts=None, scores_map=None) -> list[dict]:
    """Genera alertas. Acepta datos pre-calculados para evitar queries repetidas."""
    if accounts is None:
        accounts = CSAccount.query.all()
    if scores_map is None:
        scores_map = calcular_health_scores_batch(accounts)

    # Batch: tareas abiertas por cuenta
    account_ids = [a.id for a in accounts]
    tareas_all = CSTask.query.filter(
        CSTask.account_id.in_(account_ids), CSTask.completada.is_(False)
    ).all()
    tareas_by_acc = {}
    for t in tareas_all:
        tareas_by_acc.setdefault(str(t.account_id), []).append(t)

    # Batch: citas últimos 30 días
    hace_30 = datetime.now() - timedelta(days=30)
    citas_recientes = CSAppointment.query.filter(
        CSAppointment.account_id.in_(account_ids),
        CSAppointment.fecha_inicio >= hace_30,
    ).all()
    citas_by_acc = {}
    for c in citas_recientes:
        citas_by_acc.setdefault(str(c.account_id), []).append(c)

    # Batch: QBR completados
    qbr_completados = set()
    qbr_rows = CSTask.query.filter(
        CSTask.account_id.in_(account_ids),
        CSTask.tipo == "QBR", CSTask.completada.is_(True),
    ).with_entities(CSTask.account_id).all()
    for (acc_id,) in qbr_rows:
        qbr_completados.add(str(acc_id))

    alertas = []

    for acc in accounts:
        key = str(acc.id)
        hs = scores_map.get(key, {"score": 0, "categoria": "Riesgo"})
        score = hs["score"]
        categoria = hs["categoria"]
        tareas_abiertas = tareas_by_acc.get(key, [])
        tipos_tareas = set(t.tipo for t in tareas_abiertas)
        kam_nombre = acc.kam.nombre if acc.kam else "Sin KAM"

        # Regla 1: Cuenta nueva sin QBR
        if acc.es_cuenta_nueva and "QBR" not in tipos_tareas and key not in qbr_completados:
            alertas.append({
                "cuenta": acc.nombre, "account_id": key, "kam": kam_nombre,
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
                    "cuenta": acc.nombre, "account_id": key, "kam": kam_nombre,
                    "tipo": "cobranza_alta",
                    "titulo": f"{pct_pendiente:.0%} pendiente de cobranza",
                    "detalle": f"${pendiente:,.0f} pendiente de ${facturacion:,.0f} facturado Q1.",
                    "severidad": "critica" if pct_pendiente > 0.60 else "alta",
                    "accion": "Seguimiento de cobranza",
                })

        # Regla 3: Sin citas completadas en último mes
        citas_acc = citas_by_acc.get(key, [])
        terminadas_recientes = sum(1 for c in citas_acc if c.estatus == "Terminada")
        if len(citas_acc) > 0 and terminadas_recientes == 0:
            alertas.append({
                "cuenta": acc.nombre, "account_id": key, "kam": kam_nombre,
                "tipo": "sin_citas_completadas",
                "titulo": "Sin citas completadas en último mes",
                "detalle": f"{len(citas_acc)} citas sin terminar.",
                "severidad": "alta", "accion": "Revisar operación",
            })
        elif len(citas_acc) == 0 and acc.sucursales > 0:
            alertas.append({
                "cuenta": acc.nombre, "account_id": key, "kam": kam_nombre,
                "tipo": "sin_actividad",
                "titulo": "Sin actividad operativa en último mes",
                "detalle": f"Sin citas en 30 días ({acc.sucursales} sucursales).",
                "severidad": "media", "accion": "Verificar calendario",
            })

        # Regla 4: Riesgo sin tarea
        if categoria == "Riesgo" and not tareas_abiertas:
            alertas.append({
                "cuenta": acc.nombre, "account_id": key, "kam": kam_nombre,
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
                    "cuenta": acc.nombre, "account_id": key, "kam": kam_nombre,
                    "tipo": "pre_riesgo",
                    "titulo": f"Riesgo de caer a rojo (score {score})",
                    "detalle": f"{pct_pend:.0%} pendiente.",
                    "severidad": "alta", "accion": "Intervención preventiva",
                })

    sev_order = {"critica": 0, "alta": 1, "media": 2}
    alertas.sort(key=lambda a: (sev_order.get(a["severidad"], 9), a["cuenta"]))
    return alertas


def alertas_por_cuenta(account_id: str) -> list[dict]:
    acc = db.session.get(CSAccount, account_id)
    if not acc:
        return []
    return generar_alertas(accounts=[acc])

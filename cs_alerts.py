# cs_alerts.py
"""Motor de alertas para CS Dashboard — optimizado con queries SQL agregados."""
from datetime import datetime, date, timedelta
from sqlalchemy import func, case
from extensions import db
from models import CSAccount, CSTask, CSAppointment, CSInvoice
from cs_health_score import calcular_health_scores_batch


def generar_alertas(accounts=None, scores_map=None) -> list[dict]:
    if accounts is None:
        accounts = CSAccount.query.all()
    if scores_map is None:
        scores_map = calcular_health_scores_batch(accounts)

    account_ids = [a.id for a in accounts]
    if not account_ids:
        return []

    # Batch 1: tareas abiertas agrupadas por cuenta y tipo
    tareas_rows = (
        db.session.query(
            CSTask.account_id,
            CSTask.tipo,
            func.count(CSTask.id),
        )
        .filter(CSTask.account_id.in_(account_ids), CSTask.completada.is_(False))
        .group_by(CSTask.account_id, CSTask.tipo)
        .all()
    )
    tareas_by_acc = {}
    for acc_id, tipo, cnt in tareas_rows:
        key = str(acc_id)
        if key not in tareas_by_acc:
            tareas_by_acc[key] = {"tipos": set(), "total": 0}
        tareas_by_acc[key]["tipos"].add(tipo)
        tareas_by_acc[key]["total"] += cnt

    # Batch 2: citas últimos 30 días — solo conteos por estatus (no cargar rows)
    hace_30 = datetime.now() - timedelta(days=30)
    citas_rows = (
        db.session.query(
            CSAppointment.account_id,
            func.count(CSAppointment.id),
            func.sum(case((CSAppointment.estatus == "Terminada", 1), else_=0)),
        )
        .filter(
            CSAppointment.account_id.in_(account_ids),
            CSAppointment.fecha_inicio >= hace_30,
        )
        .group_by(CSAppointment.account_id)
        .all()
    )
    citas_by_acc = {}
    for acc_id, total, terminadas in citas_rows:
        citas_by_acc[str(acc_id)] = {"total": total, "terminadas": int(terminadas or 0)}

    # Batch 3: QBR completados
    qbr_rows = (
        db.session.query(CSTask.account_id)
        .filter(
            CSTask.account_id.in_(account_ids),
            CSTask.tipo == "QBR",
            CSTask.completada.is_(True),
        )
        .distinct()
        .all()
    )
    qbr_completados = {str(r[0]) for r in qbr_rows}

    # Batch 4: vencido real por cuenta (facturas que pasaron fecha_vencimiento)
    hoy = date.today()
    vencido_rows = (
        db.session.query(
            CSInvoice.account_id,
            func.coalesce(func.sum(CSInvoice.pendiente), 0),
        )
        .filter(
            CSInvoice.account_id.in_(account_ids),
            CSInvoice.pendiente > 0,
            CSInvoice.fecha_vencimiento < hoy,
        )
        .group_by(CSInvoice.account_id)
        .all()
    )
    vencido_by_acc = {str(r[0]): float(r[1]) for r in vencido_rows}

    alertas = []

    for acc in accounts:
        key = str(acc.id)
        hs = scores_map.get(key, {"score": 0, "categoria": "Riesgo"})
        score = hs["score"]
        categoria = hs["categoria"]
        tareas_info = tareas_by_acc.get(key, {"tipos": set(), "total": 0})
        citas_info = citas_by_acc.get(key, {"total": 0, "terminadas": 0})
        kam_nombre = acc.kam.nombre if acc.kam else "Sin KAM"

        # Regla 1: Cuenta nueva sin QBR
        if acc.es_cuenta_nueva and "QBR" not in tareas_info["tipos"] and key not in qbr_completados:
            alertas.append({
                "cuenta": acc.nombre, "account_id": key, "kam": kam_nombre,
                "tipo": "sin_qbr", "titulo": "Cuenta nueva sin QBR agendado",
                "detalle": f"{acc.nombre} es cuenta nueva sin QBR.",
                "severidad": "alta", "accion": "Agendar QBR",
            })

        # Regla 2: >30% VENCIDO de cobranza (solo facturas que pasaron fecha de vencimiento)
        facturacion = float(acc.facturacion_q1 or 0)
        pendiente = float(acc.pendiente_q1 or 0)
        # Calcular solo vencido real (pasó fecha_vencimiento)
        vencido = vencido_by_acc.get(key, 0)
        if facturacion > 0 and vencido > 0:
            pct_vencido = vencido / facturacion
            if pct_vencido > 0.30:
                alertas.append({
                    "cuenta": acc.nombre, "account_id": key, "kam": kam_nombre,
                    "tipo": "cobranza_alta",
                    "titulo": f"{pct_vencido:.0%} vencido de cobranza",
                    "detalle": f"${vencido:,.0f} vencido de ${facturacion:,.0f} (excluye facturas en plazo de credito).",
                    "severidad": "critica" if pct_vencido > 0.60 else "alta",
                    "accion": "Seguimiento de cobranza",
                })

        # Regla 3: Sin citas completadas en último mes
        if citas_info["total"] > 0 and citas_info["terminadas"] == 0:
            alertas.append({
                "cuenta": acc.nombre, "account_id": key, "kam": kam_nombre,
                "tipo": "sin_citas_completadas",
                "titulo": "Sin citas completadas en último mes",
                "detalle": f"{citas_info['total']} citas sin terminar.",
                "severidad": "alta", "accion": "Revisar operación",
            })
        elif citas_info["total"] == 0 and acc.sucursales > 0:
            alertas.append({
                "cuenta": acc.nombre, "account_id": key, "kam": kam_nombre,
                "tipo": "sin_actividad",
                "titulo": "Sin actividad operativa en último mes",
                "detalle": f"Sin citas en 30 días ({acc.sucursales} sucursales).",
                "severidad": "media", "accion": "Verificar calendario",
            })

        # Regla 4: Riesgo sin tarea
        if categoria == "Riesgo" and tareas_info["total"] == 0:
            alertas.append({
                "cuenta": acc.nombre, "account_id": key, "kam": kam_nombre,
                "tipo": "riesgo_sin_tarea",
                "titulo": f"Cuenta en RIESGO (score {score}) sin plan",
                "detalle": f"Score {score}/100 sin tareas.",
                "severidad": "critica", "accion": "Crear plan de acción",
            })

        # Regla 5: Pre-riesgo (solo si hay vencido real, no por cobrar en plazo)
        if categoria == "Atención" and facturacion > 0 and vencido > 0:
            pct_vencido = vencido / facturacion
            if pct_vencido > 0.30:
                alertas.append({
                    "cuenta": acc.nombre, "account_id": key, "kam": kam_nombre,
                    "tipo": "pre_riesgo",
                    "titulo": f"Riesgo de caer a rojo (score {score})",
                    "detalle": f"{pct_vencido:.0%} vencido (excluye facturas en plazo).",
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

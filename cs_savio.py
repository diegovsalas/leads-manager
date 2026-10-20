"""Contrato de Savio por cuenta de CS.  FEAT-2026-10-20

Savio es donde vive el contrato real: las suscripciones vigentes de cada
cliente. Este módulo lo lleva a las pantallas donde se trabaja (Hoy,
Seguimiento semanal, Trabajo de cuenta, Reporte semanal), que desde la
simplificación de CS solo mostraban tareas.

Se calcula en vivo desde las tablas de Savio con el MISMO criterio que
savio_sync.bridge_savio_to_cs_mrr, y no se lee CSAccount.mrr: esa columna
también se edita a mano en Clientes, así que no garantiza que el número
venga de Savio.

Bases distintas, a propósito explícitas:
  - mrr: suma de SavioSubscription.mrr, SIN IVA (así lo guarda Savio).
  - La facturación del reporte semanal sale de facturas, CON IVA. Por eso
    la meta sugerida es mrr × 1.16: comparar sin IVA contra con IVA haría
    ver a toda cuenta 16% arriba de su meta.
"""
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo

from extensions import db
from models import CustomerMaster, CustomerRfc, SavioSubscription

MX = ZoneInfo("America/Monterrey")
IVA = Decimal("1.16")
# Diferencia entre el contrato cargado y Savio a partir de la cual se avisa.
# Debajo de esto suelen ser redondeos o un servicio movido de ciclo.
TOLERANCIA = Decimal("0.10")

_UNIDADES = {"aromatex": "Aromatex", "pestex": "Pestex", "weldex": "Weldex",
             "weldu": "Weldu", "comercializadora": "Comercializadora"}


def _pesos(valor):
    return Decimal(valor).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def contratos(account_ids):
    """{account_id: contrato} para las cuentas pedidas, en 3 consultas.

    contrato = {vinculada, mrr, arr, meta_sugerida, suscripciones, unidades}
    vinculada es False cuando la cuenta no tiene razón social de Savio
    ligada: entonces mrr es None, no 0, porque no sabemos cuánto es.
    """
    ids = [a for a in account_ids if a]
    vacio = {str(a): dict(vinculada=False, mrr=None, arr=None, meta_sugerida=None,
                          suscripciones=0, unidades=[]) for a in ids}
    if not ids:
        return vacio

    master_a_cuenta = {m.id: str(m.cs_account_id) for m in
                       CustomerMaster.query.filter(CustomerMaster.cs_account_id.in_(ids)).all()}
    cliente_a_cuenta = {}
    if master_a_cuenta:
        for r in CustomerRfc.query.filter(CustomerRfc.master_id.in_(list(master_a_cuenta)),
                                          CustomerRfc.savio_customer_id.isnot(None)).all():
            cliente_a_cuenta[str(r.savio_customer_id)] = master_a_cuenta[r.master_id]

    for cuenta in set(cliente_a_cuenta.values()):
        vacio[cuenta].update(vinculada=True, mrr=Decimal("0"))

    if cliente_a_cuenta:
        hoy = datetime.now(MX).date()
        subs = (SavioSubscription.query
                .filter(SavioSubscription.customer_id.in_(list(cliente_a_cuenta)))
                .filter(SavioSubscription.sum_mrr.is_(True))
                .filter(SavioSubscription.start_date <= hoy)
                .filter(db.or_(SavioSubscription.contract_end_date.is_(None),
                               SavioSubscription.contract_end_date > hoy))
                .all())
        for s in subs:
            c = vacio[cliente_a_cuenta[str(s.customer_id)]]
            c["mrr"] += Decimal(str(s.mrr or 0))
            c["suscripciones"] += 1
            un = _UNIDADES.get((s.unit or "").lower())
            if un and un not in c["unidades"]:
                c["unidades"].append(un)

    for c in vacio.values():
        if c["mrr"] is not None:
            c["mrr"] = _pesos(c["mrr"])
            c["arr"] = _pesos(c["mrr"] * 12)
            c["meta_sugerida"] = _pesos(c["mrr"] * IVA) if c["mrr"] > 0 else None
        c["unidades"].sort()
    return vacio


def contrato(account_id):
    return contratos([account_id])[str(account_id)]


def conciliar(savio, resumen):
    """Compara el contrato cargado (CSV) contra Savio, en monto mensual.

    El CSV trae el importe por ciclo; solo se puede llevar a mensual si todos
    sus servicios comparten ciclo (resumen['cada_meses']). Devuelve None si no
    hay con qué comparar, o {mensual_contrato, mrr, diferencia, porcentaje,
    cuadra, mensaje}.
    """
    if not resumen or not savio or not savio.get("vinculada"):
        return None
    meses = resumen.get("cada_meses")
    try:
        importe = Decimal(str(resumen.get("importe_activos") or 0))
    except Exception:
        return None
    if not meses or importe <= 0:
        return None

    mensual = _pesos(importe / Decimal(meses))
    mrr = savio["mrr"] or Decimal("0")
    if mrr == 0:
        return dict(mensual_contrato=mensual, mrr=mrr, diferencia=mensual, porcentaje=None,
                    cuadra=False,
                    mensaje="El contrato cargado tiene servicios vigentes, pero Savio no tiene "
                            "suscripción activa: falta darla de alta o el contrato ya no aplica.")
    diferencia = _pesos(mensual - mrr)
    porcentaje = (diferencia / mrr).quantize(Decimal("0.001"))
    cuadra = abs(porcentaje) <= TOLERANCIA
    if cuadra:
        mensaje = "El contrato cargado y Savio coinciden."
    elif diferencia > 0:
        mensaje = ("El contrato cargado es mayor que lo registrado en Savio: puede faltar "
                   "una suscripción o un servicio sin facturar.")
    else:
        mensaje = ("Savio registra más que el contrato cargado: puede haber un servicio fuera "
                   "del contrato o una versión del contrato desactualizada.")
    return dict(mensual_contrato=mensual, mrr=mrr, diferencia=diferencia,
                porcentaje=porcentaje, cuadra=cuadra, mensaje=mensaje)
